import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI

from app.services import player_feedback as feedback
from app.services import feishu_alerts
from app.api.routes.feedback import router, MAX_BODY


class Remote:
    def __init__(self):
        self.uploads = self.creates = 0
        self.record = ""
        self.fail = False

    async def authenticate(self):
        pass

    async def find(self, report_id):
        return self.record

    async def upload(self, row):
        self.uploads += 1
        return "file-token"

    async def create(self, row):
        self.creates += 1
        if self.fail:
            raise TimeoutError()
        self.record = "record-id"
        return self.record


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "feedback.db"
        self.payload = {"feedback_id": "a" * 32, "description": "验证：按钮没有反应", "platform": "windows"}
        self.image = b"\xff\xd8\xffTEST\xff\xd9"

    def tearDown(self):
        self.temp.cleanup()

    def accept(self, **changes):
        payload = dict(self.payload, **changes)
        return feedback.accept(payload, self.image, "owner", "ip", self.path)

    def test_reproduction_optional_and_bounded(self):
        self.assertEqual(feedback.validate(self.payload, b"").get("reproduction", ""), "")
        self.assertEqual(feedback.validate(dict(self.payload, reproduction="  顶着猫在河里钓鱼  "), b"")["reproduction"], "顶着猫在河里钓鱼")
        with self.assertRaises(feedback.FeedbackRejected):
            feedback.validate(dict(self.payload, reproduction="x" * 2001), b"")

    def test_atomic_receipt_and_duplicate(self):
        self.assertEqual(self.accept()["status"], "accepted")
        self.assertEqual(self.accept()["status"], "accepted")
        with feedback.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM feedback").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT image FROM feedback").fetchone()[0], self.image)
        with self.assertRaises(feedback.FeedbackRejected) as error:
            self.accept(description="changed")
        self.assertEqual(error.exception.status, 409)

    def test_validation_and_limit(self):
        for changes in ({"description": "  "}, {"description": "x" * 2001}, {"feedback_id": "../x"}, {"errors": "猫" * 12000}):
            with self.assertRaises(feedback.FeedbackRejected):
                self.accept(**changes)
        self.accept()
        self.accept(feedback_id="b" * 32)
        self.accept(feedback_id="c" * 32)
        with self.assertRaises(feedback.FeedbackRejected) as error:
            self.accept(feedback_id="d" * 32)
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(self.accept()["status"], "accepted")

    async def test_retry_reuses_attachment_and_recovers_uncertain_create(self):
        self.accept()
        remote = Remote()
        remote.fail = True
        await feedback.deliver_one(self.path, remote)
        with feedback.connect(self.path) as db:
            row = db.execute("SELECT * FROM feedback").fetchone()
            self.assertEqual(row["state"], "pending")
            self.assertEqual(row["file_token"], "file-token")
        # Server created the row but the response was lost; search reconciles it.
        remote.record = "existing-record"
        feedback.update(self.path, "a" * 32, due=0)
        await feedback.deliver_one(self.path, remote)
        self.assertEqual(remote.uploads, 1)
        self.assertEqual(remote.creates, 1)
        with feedback.connect(self.path) as db:
            row = db.execute("SELECT * FROM feedback").fetchone()
            self.assertEqual(row["state"], "sent")
            self.assertEqual(row["record_id"], "existing-record")

    async def test_lease_recovery_and_cleanup(self):
        self.accept()
        self.assertIsNotNone(feedback.claim(self.path))
        self.assertIsNone(feedback.claim(self.path))
        feedback.update(self.path, "a" * 32, due=0)
        self.assertIsNotNone(feedback.claim(self.path))
        feedback.update(self.path, "a" * 32, state="sent", sent_at=time.time() - 8 * 86400)
        feedback.claim(self.path)
        with feedback.connect(self.path) as db:
            self.assertIsNone(db.execute("SELECT image FROM feedback").fetchone()[0])
        self.assertEqual(self.accept()["delivery"], "sent")

    async def test_real_http_multipart_encryption_and_size_limit(self):
        app = FastAPI()
        app.include_router(router)
        key = Fernet.generate_key()
        encrypted = Fernet(key).encrypt(json.dumps(self.payload).encode()).decode()
        with patch.dict(os.environ, {"PAW_FERNET_KEY": key.decode(), "FEEDBACK_DB_PATH": str(self.path)}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/feedback", headers={"X-User-ID": "owner"},
                    data={"metadata": encrypted}, files={"screenshot": ("a.jpg", self.image, "image/jpeg")})
                self.assertEqual(response.status_code, 202, response.text)
                response = await client.post("/feedback", headers={"X-User-ID": "owner"}, content=b"x" * (MAX_BODY + 1))
                self.assertEqual(response.status_code, 413)
                response = await client.post("/feedback", headers={"X-User-ID": "owner"}, data={"metadata": "not-encrypted"})
                self.assertEqual(response.status_code, 400)
                app.state.blacklist = {"owner"}
                response = await client.post("/feedback", headers={"X-User-ID": "owner"})
                self.assertEqual(response.status_code, 403)

    async def test_missing_credentials_stays_pending(self):
        self.accept()
        with patch.dict(os.environ, {"FEISHU_FEEDBACK_APP_TOKEN": ""}):
            await feedback.deliver_one(self.path)
        with feedback.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT state FROM feedback").fetchone()[0], "pending")


class FeedbackNotificationTests(unittest.IsolatedAsyncioTestCase):
    setUp = FeedbackTests.setUp
    tearDown = FeedbackTests.tearDown
    accept = FeedbackTests.accept

    async def test_notify_after_table_success_retries_without_rewriting_record(self):
        self.accept(reproduction="顶着一只猫，14:00–15:00 时在河里钓鱼")
        sender = AsyncMock(side_effect=[False, True])
        self.assertFalse(await feedback.notify_one(self.path, sender))
        remote = Remote()
        with patch.dict(os.environ, {"FEISHU_FEEDBACK_APP_TOKEN": "base-test", "FEISHU_FEEDBACK_TABLE_ID": "table-test"}):
            await feedback.deliver_one(self.path, remote)
        await feedback.notify_one(self.path, sender)
        with feedback.connect(self.path) as db:
            notification = dict(db.execute("SELECT * FROM feedback_notifications").fetchone())
            self.assertEqual(notification["state"], "pending")
            self.assertGreater(notification["due"], time.time())
            self.assertEqual(db.execute("SELECT state FROM feedback").fetchone()[0], "sent")
            db.execute("UPDATE feedback_notifications SET due=0")
        await feedback.notify_one(self.path, sender)
        self.assertFalse(await feedback.notify_one(self.path, sender))
        self.assertFalse(await feedback.deliver_one(self.path, remote))
        self.assertEqual((remote.creates, remote.uploads), (1, 1))
        self.assertEqual(sender.await_count, 2)
        text, report_id = sender.await_args.args
        self.assertIn("顶着一只猫", text)
        self.assertIn("验证：按钮没有反应", text)
        self.assertIn("https://feishu.cn/base/base-test?table=table-test&record=record-id", text)
        self.assertEqual(report_id, "a" * 32)
        self.accept(reproduction="顶着一只猫，14:00–15:00 时在河里钓鱼")
        self.assertFalse(await feedback.notify_one(self.path, sender))

    async def test_notification_failure_and_lease_survive_restart(self):
        self.accept()
        await feedback.deliver_one(self.path, Remote())
        first = feedback.claim_notification(self.path)
        self.assertIsNotNone(first)
        self.assertIsNone(feedback.claim_notification(self.path))
        with feedback.connect(self.path) as db:
            db.execute("UPDATE feedback_notifications SET due=0")
        sender = AsyncMock(side_effect=TimeoutError())
        await feedback.notify_one(self.path, sender)
        with feedback.connect(self.path) as db:
            row = db.execute("SELECT * FROM feedback_notifications").fetchone()
            self.assertEqual(row["state"], "pending")
            self.assertEqual(row["last_error"], "TimeoutError")
            self.assertIn("未填写", row["text"])

    async def test_migration_does_not_notify_old_completed_reports(self):
        self.accept()
        with feedback.connect(self.path) as db:
            db.execute("UPDATE feedback SET state='sent'")
            db.execute("DROP TABLE feedback_notifications")
        sender = AsyncMock()
        self.assertFalse(await feedback.notify_one(self.path, sender))
        sender.assert_not_awaited()

    async def test_message_uses_error_recipient_and_stable_deduplication_uuid(self):
        requests = []
        async def respond(request):
            requests.append(request)
            if "tenant_access_token" in str(request.url):
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "test-token"})
            return httpx.Response(200, json={"code": 0})
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.dict(os.environ, {"FEISHU_BOT_API_KEY": "test-app", "FEISHU_BOT_API_SECRET": "test-secret",
                                         "FEISHU_CHAT_ID": "test-error-recipient", "FEISHU_RECEIVE_ID_TYPE": "chat_id",
                                         "FEISHU_ERROR_LOG_ALERTS": "false"}), patch.object(feishu_alerts.httpx, "AsyncClient", return_value=client):
            self.assertTrue(await feishu_alerts.send_player_feedback_alert("验证：新反馈", "report"))
        body = json.loads(requests[-1].content)
        self.assertEqual(body["receive_id"], "test-error-recipient")
        self.assertEqual(json.loads(body["content"])["text"], "验证：新反馈")
        self.assertEqual(body["uuid"], __import__("hashlib").sha256(b"player-feedback:report").hexdigest()[:32])


    async def test_receipt_and_notification_enqueue_are_atomic(self):
        self.accept()
        remote = Remote()
        with feedback.connect(self.path) as db:
            db.execute("CREATE TRIGGER reject_notice BEFORE INSERT ON feedback_notifications BEGIN SELECT RAISE(ABORT,'test failure'); END")
        await feedback.deliver_one(self.path, remote)
        with feedback.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT state FROM feedback").fetchone()[0], "pending")
            self.assertEqual(db.execute("SELECT count(*) FROM feedback_notifications").fetchone()[0], 0)
            db.execute("DROP TRIGGER reject_notice")
            db.execute("UPDATE feedback SET due=0")
        await feedback.deliver_one(self.path, remote)
        self.assertEqual(remote.creates, 1)
        with feedback.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT state FROM feedback").fetchone()[0], "sent")
            self.assertEqual(db.execute("SELECT count(*) FROM feedback_notifications").fetchone()[0], 1)

    def test_notification_backlog_applies_acceptance_budget(self):
        with feedback.connect(self.path) as db:
            db.executemany("INSERT INTO feedback_notifications(id,text) VALUES(?,?)", ((str(i), "test") for i in range(1000)))
        with self.assertRaises(feedback.FeedbackRejected) as error:
            self.accept()
        self.assertEqual(error.exception.status, 503)
