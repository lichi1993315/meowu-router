"""Durable player feedback. The database transaction includes the JPEG before ACK."""
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import contextmanager

import httpx

from app.core.config import OUTPUT_DIR
from app.core.logging import log
from app.services import feishu_alerts

MAX_IMAGE = 2 * 1024 * 1024
MAX_META = 48 * 1024
API = "https://open.feishu.cn/open-apis"


def database() -> Path:
    return Path(os.getenv("FEEDBACK_DB_PATH", str(OUTPUT_DIR / "feedback.sqlite3")))


@contextmanager
def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous=FULL")
    db.execute("""CREATE TABLE IF NOT EXISTS feedback (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, ip TEXT NOT NULL,
        payload TEXT NOT NULL, fingerprint TEXT NOT NULL, image BLOB,
        state TEXT NOT NULL DEFAULT 'pending', received REAL NOT NULL,
        due REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
        file_token TEXT NOT NULL DEFAULT '', record_id TEXT NOT NULL DEFAULT '',
        sent_at REAL, last_error TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS feedback_notifications (
        id TEXT PRIMARY KEY, text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        due REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
        sent_at REAL, last_error TEXT NOT NULL DEFAULT '')""")
    db.execute("CREATE INDEX IF NOT EXISTS feedback_notifications_due ON feedback_notifications(state,due)")
    for name, columns in (("due", "state,due,received"), ("owner", "owner,received"),
                          ("ip", "ip,received"), ("sent", "state,sent_at")):
        db.execute(f"CREATE INDEX IF NOT EXISTS feedback_{name} ON feedback({columns})")
    db.execute("CREATE INDEX IF NOT EXISTS feedback_images_to_clean ON feedback(sent_at) WHERE state='sent' AND image IS NOT NULL")
    try:
        with db:
            yield db
    finally:
        db.close()


class FeedbackRejected(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def validate(payload: dict, image: bytes) -> dict:
    if not isinstance(payload, dict):
        raise FeedbackRejected(400, "Invalid metadata")
    result = {}
    limits = {"feedback_id": 32, "description": 2000, "reproduction": 2000, "contact": 100, "captured_at": 64,
              "version": 160, "platform": 32, "scene": 160, "resolution": 32,
              "player_id": 128, "session_id": 128, "errors": 32768}
    for field, limit in limits.items():
        value = payload.get(field, "")
        if not isinstance(value, str) or len(value) > limit:
            raise FeedbackRejected(400, f"Invalid {field}")
        result[field] = value.strip()
    if not re.fullmatch(r"[0-9a-f]{32}", result["feedback_id"]) or not result["description"]:
        raise FeedbackRejected(400, "Missing feedback ID or description")
    if len(result["errors"].encode()) > 32768 or len(json.dumps(result, ensure_ascii=False).encode()) > MAX_META:
        raise FeedbackRejected(413, "Metadata too large")
    if len(image) > MAX_IMAGE:
        raise FeedbackRejected(413, "Screenshot too large")
    if image and (not image.startswith(b"\xff\xd8\xff") or not image.endswith(b"\xff\xd9")):
        raise FeedbackRejected(400, "Screenshot must be JPEG")
    # Empty optional field preserves fingerprints of reports accepted before this field existed.
    for field in ("reproduction", "contact"):
        if not result[field]:
            result.pop(field)
    return result


def accept(payload: dict, image: bytes, owner: str, ip: str, path: Path | None = None) -> dict:
    payload = validate(payload, image)
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(body.encode() + image).hexdigest()
    now = time.time()
    with connect(path or database()) as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT owner,fingerprint,state FROM feedback WHERE id=?", (payload["feedback_id"],)).fetchone()
        if previous:
            if previous["owner"] != owner or previous["fingerprint"] != fingerprint:
                raise FeedbackRejected(409, "Feedback ID already used with different content")
            return {"status": "accepted", "feedback_id": payload["feedback_id"], "delivery": previous["state"]}
        for column, value in (("owner", owner), ("ip", ip)):
            count = db.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM feedback WHERE {column}=? AND received>? LIMIT 3)", (value, now - 60)).fetchone()[0]
            if count >= 3:
                raise FeedbackRejected(429, "Please wait before sending another feedback")
        pending = db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM feedback WHERE state IN ('pending','sending') LIMIT 1000)").fetchone()[0]
        pending += db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM feedback_notifications WHERE state IN ('pending','sending') LIMIT 1000)").fetchone()[0]
        if pending >= 1000:
            raise FeedbackRejected(503, "Feedback queue full; please retry later")
        db.execute("INSERT INTO feedback(id,owner,ip,payload,fingerprint,image,received) VALUES(?,?,?,?,?,?,?)",
                   (payload["feedback_id"], owner, ip, body, fingerprint, image or None, now))
    return {"status": "accepted", "feedback_id": payload["feedback_id"], "delivery": "pending"}


def claim(path: Path):
    now = time.time()
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM feedback WHERE state IN ('pending','sending') AND due<=? ORDER BY received LIMIT 1", (now,)).fetchone()
        if row:
            db.execute("UPDATE feedback SET state='sending',due=? WHERE id=?", (now + 300, row["id"]))
        # Bounded cleanup; retain the receipt and fingerprint for retry deduplication.
        db.execute("UPDATE feedback SET image=NULL WHERE id IN (SELECT id FROM feedback WHERE state='sent' AND sent_at<? AND image IS NOT NULL LIMIT 10)", (now - 7 * 86400,))
        return dict(row) if row else None


def update(path: Path, report_id: str, **values):
    with connect(path) as db:
        db.execute("UPDATE feedback SET " + ",".join(k + "=?" for k in values) + " WHERE id=?", (*values.values(), report_id))


def notification_text(row: dict, record: str) -> str:
    """Plain-text summary with a durable record link; screenshots remain in the Base."""
    payload = json.loads(row["payload"])
    app = os.getenv("FEISHU_FEEDBACK_APP_TOKEN", "").strip()
    table = os.getenv("FEISHU_FEEDBACK_TABLE_ID", "").strip()
    link = f"https://feishu.cn/base/{app}?table={table}&record={record}"
    return "\n".join([
        "【喵呜岛 · 新的玩家 Bug 反馈】",
        "反馈编号：" + row["id"],
        "版本：" + payload["version"] + " ｜平台：" + payload["platform"],
        "玩家：" + (payload["player_id"] or row["owner"]),
        "场景：" + payload["scene"],
        "联系方式：" + feishu_alerts._clean_text(payload.get("contact") or "未填写", max_chars=100),
        "", "问题描述：", feishu_alerts._clean_text(payload["description"], max_chars=1200),
        "", "复现办法：", feishu_alerts._clean_text(payload.get("reproduction") or "未填写", max_chars=600),
        "", "截图：" + ("已附在表格记录中" if row["file_token"] or row["image"] else "未附截图"),
        "查看完整反馈：" + link,
    ])


def finish_record(path: Path, row: dict, record: str):
    """Commit the table receipt and notification outbox together; retries cannot enqueue twice."""
    with connect(path) as db:
        db.execute("UPDATE feedback SET state='sent',record_id=?,sent_at=?,last_error='' WHERE id=?",
                   (record, time.time(), row["id"]))
        db.execute("INSERT OR IGNORE INTO feedback_notifications(id,text) VALUES(?,?)",
                   (row["id"], notification_text(row, record)))


def claim_notification(path: Path):
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM feedback_notifications WHERE state IN ('pending','sending') AND due<=? ORDER BY due LIMIT 1",
                         (time.time(),)).fetchone()
        if row:
            db.execute("UPDATE feedback_notifications SET state='sending',due=? WHERE id=?", (time.time() + 300, row["id"]))
        return dict(row) if row else None


async def notify_one(path: Path | None = None, sender=None) -> bool:
    """Retry notifications independently: a failed message never rewrites the table or attachment."""
    path = path or database()
    row = await asyncio.to_thread(claim_notification, path)
    if row is None:
        return False
    error = ""
    try:
        sent = await (sender or feishu_alerts.send_player_feedback_alert)(row["text"], row["id"])
        if not sent:
            error = "Feishu not acknowledged"
    except Exception as exc:
        sent = False
        error = type(exc).__name__

    def finish():
        with connect(path) as db:
            db.execute("UPDATE feedback_notifications SET state=?,attempts=?,due=?,sent_at=?,last_error=? WHERE id=?",
                       ("sent" if sent else "pending", row["attempts"] + 1,
                        time.time() + min(300, 10 * 2 ** min(row["attempts"], 5)),
                        time.time() if sent else None, error, row["id"]))
    await asyncio.to_thread(finish)
    if error:
        log(f"[WARNING] Feedback {row['id']} notification deferred: {error}")
    return True


class FeishuFeedbackClient:
    """Credentials stay server-side; API errors retain codes, never tokens or request bodies."""
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.app = os.getenv("FEISHU_FEEDBACK_APP_TOKEN", "").strip()
        self.table = os.getenv("FEISHU_FEEDBACK_TABLE_ID", "").strip()
        self.token = ""

    async def authenticate(self):
        key = os.getenv("FEISHU_BOT_API_KEY", "").strip()
        secret = os.getenv("FEISHU_BOT_API_SECRET", "").strip()
        if not all((key, secret, self.app, self.table)):
            raise RuntimeError("feedback_configuration_missing")
        data = await self.call("POST", "/auth/v3/tenant_access_token/internal", json={"app_id": key, "app_secret": secret})
        self.token = data["tenant_access_token"]

    async def call(self, method, route, **kwargs):
        headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        response = await self.client.request(method, API + route, headers=headers, **kwargs)
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError("feishu_code_" + str(body.get("code")))
        return body

    @property
    def records(self):
        return f"/bitable/v1/apps/{self.app}/tables/{self.table}/records"

    async def find(self, report_id):
        data = await self.call("POST", self.records + "/search", params={"page_size": 1},
                               json={"filter": {"conjunction": "and", "conditions": [
                                   {"field_name": "反馈编号", "operator": "is", "value": [report_id]}]}})
        items = data.get("data", {}).get("items", [])
        return items[0]["record_id"] if items else ""

    async def upload(self, row):
        data = await self.call("POST", "/drive/v1/medias/upload_all",
            data={"file_name": row["id"] + ".jpg", "parent_type": "bitable_image",
                  "parent_node": self.app, "size": str(len(row["image"]))},
            files={"file": (row["id"] + ".jpg", row["image"], "image/jpeg")})
        return data["data"]["file_token"]

    async def create(self, row):
        payload = json.loads(row["payload"])
        fields = {"反馈编号": row["id"], "提交时间": int(row["received"] * 1000),
                  "截图时间": payload["captured_at"], "问题描述": payload["description"], "复现办法": payload.get("reproduction", ""),
                  "游戏版本": payload["version"], "平台": payload["platform"],
                  "场景": payload["scene"], "分辨率": payload["resolution"],
                  "玩家标识": payload["player_id"] or row["owner"],
                  "会话标识": payload["session_id"], "近期错误摘要": payload["errors"], "处理状态": "待处理"}
        if payload.get("contact"):
            fields["联系方式"] = payload["contact"]
        if row["file_token"]:
            fields["截图"] = [{"file_token": row["file_token"]}]
        data = await self.call("POST", self.records, params={"client_token": str(uuid.UUID(hex=row["id"]))}, json={"fields": fields})
        return data["data"]["record"]["record_id"]


async def deliver_one(path: Path | None = None, remote=None) -> bool:
    path = path or database()
    row = await asyncio.to_thread(claim, path)
    if not row:
        return False
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            remote = remote or FeishuFeedbackClient(client)
            await remote.authenticate()
            record = await remote.find(row["id"])
            if not record:
                if row["image"] and not row["file_token"]:
                    row["file_token"] = await remote.upload(row)
                    await asyncio.to_thread(update, path, row["id"], file_token=row["file_token"])
                record = await remote.create(row)
            await asyncio.to_thread(finish_record, path, row, record)
    except Exception as exc:
        # Do not log response bodies, headers, or player descriptions.
        error = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        await asyncio.to_thread(update, path, row["id"], state="pending", attempts=row["attempts"] + 1,
                                due=time.time() + min(300, 10 * 2 ** min(row["attempts"], 5)), last_error=error[:120])
        log(f"[WARNING] Feedback {row['id']} delivery deferred: {error[:120]}")
    return True


async def delivery_loop():
    while True:
        try:
            for _ in range(10):
                delivered = await deliver_one()
                notified = await notify_one()
                if not delivered and not notified:
                    break
        except Exception as exc:
            log(f"[WARNING] Feedback queue: {type(exc).__name__}")
        await asyncio.sleep(5)
