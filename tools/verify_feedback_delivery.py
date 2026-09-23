#!/usr/bin/env python3
"""Explicit opt-in integration test: submit one labelled verification row to the test Base."""
import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main():
    from cryptography.fernet import Fernet
    from fastapi import FastAPI
    from app.api.routes.feedback import router
    from app.services import player_feedback as feedback

    for name in ("APP_TOKEN", "TABLE_ID"):
        os.environ["FEISHU_FEEDBACK_" + name] = os.environ["FEISHU_FEEDBACK_TEST_" + name]
    report_id = uuid.uuid4().hex
    payload = {"feedback_id": report_id, "description": "验证：截图反馈真实附件与重试去重测试", "platform": "integration-test", "version": "feedback-v1-validation", "reproduction": "顶着一只猫，14:00–15:00 时在河里钓鱼"}
    screenshot = Path(sys.argv[1]).read_bytes()
    metadata = Fernet(os.environ["PAW_FERNET_KEY"].encode()).encrypt(json.dumps(payload).encode()).decode()
    with tempfile.TemporaryDirectory() as folder:
        os.environ["FEEDBACK_DB_PATH"] = str(Path(folder) / "feedback.sqlite3")
        app = FastAPI()
        app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(2):
                response = await client.post("/feedback", headers={"X-User-ID": "feedback-validation"},
                    data={"metadata": metadata}, files={"screenshot": ("verification.jpg", screenshot, "image/jpeg")})
                assert response.status_code == 202, response.text
        await feedback.deliver_one()
        with feedback.connect(feedback.database()) as db:
            row = dict(db.execute("SELECT * FROM feedback").fetchone())
        assert row["state"] == "sent", row["last_error"]
        async with httpx.AsyncClient(timeout=30) as client:
            remote = feedback.FeishuFeedbackClient(client)
            await remote.authenticate()
            body = await remote.call("GET", remote.records + "/" + row["record_id"])
            fields = body["data"]["record"]["fields"]
            assert fields["复现办法"] == payload["reproduction"]
            assert fields["截图"][0]["file_token"] == row["file_token"]
            response = await client.get(feedback.API + "/drive/v1/medias/" + row["file_token"] + "/download",
                headers={"Authorization": "Bearer " + remote.token}, follow_redirects=True)
            response.raise_for_status()
            assert response.content == screenshot, "Attachment bytes differ"
        print(json.dumps({"status": "sent", "feedback_id": report_id, "record_id": row["record_id"],
                          "attachment_bytes": len(screenshot), "duplicate_submissions": 2, "records": 1}))


if __name__ == "__main__":
    asyncio.run(main())
