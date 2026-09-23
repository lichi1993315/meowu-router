import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI

from app.services import player_feedback as feedback
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
