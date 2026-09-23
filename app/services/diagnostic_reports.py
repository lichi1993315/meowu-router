"""Durable, idempotent acceptance for application crash summaries.

Only callers supplying report_id use this queue; legacy error logs are unchanged.
HTTP acknowledgement means stored, not delivered to Feishu.
"""
import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from app.core.config import ERROR_LOG_DIR
from app.core.logging import log
from app.services import feishu_alerts


def _connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.execute("PRAGMA synchronous=FULL")
    db.execute("""CREATE TABLE IF NOT EXISTS reports (
        id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0,
        received REAL NOT NULL, last_error TEXT NOT NULL DEFAULT '')""")
    db.execute("CREATE INDEX IF NOT EXISTS reports_due ON reports(state,due,received)")
    return db


def accept(payload: dict, headers: dict, received_at: str, body_bytes: int,
           path: Path | None = None) -> dict:
    """Persist before ACK; scope report ids by origin and retain only non-secret headers."""
    report_id = payload.get("report_id")
    if not isinstance(report_id, str) or not 1 <= len(report_id) <= 128:
        raise ValueError("invalid report_id")
    safe_headers = {k: v for k, v in headers.items() if k.lower() in {
        "x-user-id", "x-session-id", "x-client-version", "content-type", "x-encrypted"}}
    key = hashlib.sha256((str(headers.get("x-user-id", "")) + ":" + report_id).encode()).hexdigest()
    body = json.dumps(dict(payload=payload, headers=safe_headers, received_at=received_at,
                           decrypted_body_bytes=body_bytes), ensure_ascii=False)
    if len(body.encode()) > 131072:
        raise ValueError("diagnostic report exceeds 128 KiB")
    with _connect(path or ERROR_LOG_DIR / "diagnostic_reports.sqlite3") as db:
        state = "suppressed" if payload.get("client_platform") == "editor" or feishu_alerts._is_unity_dev_payload(payload, headers) else "pending"
        db.execute("INSERT OR IGNORE INTO reports(id,body,received,state) VALUES(?,?,?,?)", (key, body, time.time(), state))
        state = db.execute("SELECT state FROM reports WHERE id=?", (key,)).fetchone()[0]
    return {"status": "accepted", "report_id": report_id, "delivery": state}


def claim(path: Path, now: float):
    """Lease one report across processes; recover abandoned leases after five minutes."""
    with _connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT id,body,attempts FROM reports WHERE state IN ('pending','sending') AND due<=? ORDER BY received LIMIT 1", (now,)).fetchone()
        if row:
            db.execute("UPDATE reports SET state='sending',due=? WHERE id=?", (now + 300, row[0]))
        return row


async def deliver_one(path: Path | None = None) -> bool:
    path = path or ERROR_LOG_DIR / "diagnostic_reports.sqlite3"
    row = await asyncio.to_thread(claim, path, time.time())
    if row is None:
        return False
    key, body, attempts = row
    error = ""
    try:
        accepted = await feishu_alerts.send_error_log_alert(**json.loads(body))
    except Exception as exc:
        accepted = False
        error = type(exc).__name__
    def finish():
        with _connect(path) as db:
            db.execute("UPDATE reports SET state=?, attempts=?, due=?, last_error=? WHERE id=?",
                       ("sent" if accepted else "pending", attempts + 1,
                        time.time() + min(300, 10 * 2 ** min(attempts, 5)),
                        "" if accepted else error or "Feishu not acknowledged", key))
    await asyncio.to_thread(finish)
    return True


async def delivery_loop():
    """Bounded single-flight sender, survives server restarts through durable leases."""
    while True:
        try:
            for _ in range(10):
                if not await deliver_one():
                    break
        except Exception as exc:
            log(f"[WARNING] Diagnostic delivery queue: {type(exc).__name__}")
        await asyncio.sleep(5)
