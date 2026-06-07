import json
import os
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.feishu_alerts import _build_error_log_alert_text


class GameTelemetryEncryptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Fernet.generate_key().decode("utf-8")
        self.fernet = Fernet(self.key.encode("utf-8"))
        self.env_patch = patch.dict(os.environ, {"PAW_FERNET_KEY": self.key})
        self.env_patch.start()
        self.log_patch = patch("app.api.routes.system.log", lambda msg: None)
        self.log_patch.start()
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.client.close()
        self.log_patch.stop()
        self.env_patch.stop()

    def _encrypt(self, value) -> str:
        if isinstance(value, str):
            body = value
        else:
            body = json.dumps(value, ensure_ascii=False)
        return self.fernet.encrypt(body.encode("utf-8")).decode("utf-8")

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "text/plain",
            "X-Encrypted": "true",
            "X-User-ID": "user-1",
            "X-Session-ID": "session-1",
            "X-Client-Version": "1.2.3",
        }

    def test_encrypted_login_reaches_session_parser(self) -> None:
        captured = {}
        payload = {"session_id": "session-1", "timestamp": "2026-06-07T01:02:03Z"}

        def fake_create_task(value):
            return value

        def fake_update_session_event_log(**kwargs):
            captured["event_type"] = kwargs["event_type"]
            captured["payload"] = json.loads(kwargs["raw_body"])
            return object()

        with (
            patch("app.api.routes.system.asyncio.create_task", fake_create_task),
            patch(
                "app.api.routes.system.sessions.update_session_event_log",
                fake_update_session_event_log,
            ),
        ):
            response = self.client.post(
                "/login",
                content=self._encrypt(payload),
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["event_type"], "login")
        self.assertEqual(captured["payload"], payload)

    def test_encrypted_logoff_preserves_root_gameplay_telemetry(self) -> None:
        captured = {}
        payload = {
            "session_id": "session-1",
            "timestamp": "2026-06-07T01:02:03Z",
            "gameplay_telemetry": {"coins": 12, "level": 3},
        }

        def fake_create_task(value):
            return value

        def fake_update_session_event_log(**kwargs):
            captured["event_type"] = kwargs["event_type"]
            captured["payload"] = json.loads(kwargs["raw_body"])
            return object()

        with (
            patch("app.api.routes.system.asyncio.create_task", fake_create_task),
            patch(
                "app.api.routes.system.sessions.update_session_event_log",
                fake_update_session_event_log,
            ),
        ):
            response = self.client.post(
                "/logoff",
                content=self._encrypt(payload),
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["event_type"], "logoff")
        self.assertEqual(captured["payload"]["gameplay_telemetry"], payload["gameplay_telemetry"])

    def test_encrypted_error_log_stores_message_and_reason(self) -> None:
        captured = {}
        alerts = {}
        payload = {
            "session_id": "session-1",
            "message": "NullReferenceException",
            "reason": "inventory item missing",
            "secret_debug_dump": "do not include this field",
        }

        def fake_create_task(value):
            return value

        def fake_save_error_log_to_file(**kwargs):
            captured["payload"] = json.loads(kwargs["request_body"])
            captured["user_id"] = kwargs["user_id"]
            return object()

        def fake_send_error_log_alert(**kwargs):
            alerts.update(kwargs)
            return object()

        with (
            patch("app.api.routes.system.asyncio.create_task", fake_create_task),
            patch("app.api.routes.system.sessions.ensure_error_log_dir", lambda: None),
            patch(
                "app.api.routes.system.sessions.save_error_log_to_file",
                fake_save_error_log_to_file,
            ),
            patch(
                "app.api.routes.system.feishu_alerts.send_error_log_alert",
                fake_send_error_log_alert,
            ),
        ):
            response = self.client.post(
                "/error_log",
                content=self._encrypt(payload),
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["user_id"], "user-1")
        self.assertEqual(captured["payload"]["message"], "NullReferenceException")
        self.assertEqual(captured["payload"]["reason"], "inventory item missing")
        self.assertEqual(alerts["payload"]["message"], "NullReferenceException")
        self.assertEqual(alerts["headers"]["x-user-id"], "user-1")
        self.assertGreater(alerts["decrypted_body_bytes"], 0)

    def test_missing_encrypted_header_returns_415(self) -> None:
        headers = self._headers()
        headers.pop("X-Encrypted")
        response = self.client.post("/login", json={"session_id": "session-1"}, headers=headers)

        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["detail"], "encrypted telemetry required")

    def test_invalid_fernet_token_returns_400(self) -> None:
        response = self.client.post("/login", content="not-a-fernet-token", headers=self._headers())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "invalid encrypted telemetry body")

    def test_decrypted_non_json_body_returns_400(self) -> None:
        response = self.client.post("/login", content=self._encrypt("not json"), headers=self._headers())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "decrypted telemetry body is not json")

    def test_decrypted_non_object_body_returns_400(self) -> None:
        response = self.client.post("/login", content=self._encrypt([1, 2, 3]), headers=self._headers())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "telemetry payload must be a json object")

    def test_error_log_alert_text_is_summary_only(self) -> None:
        text = _build_error_log_alert_text(
            payload={
                "reason": "unity_error",
                "message": "NullReferenceException\nat GamePlay.ServerDataHelper.Shop.GetShopInfo",
                "secret_debug_dump": "full payload should not be included",
            },
            headers={
                "x-user-id": "user-1",
                "x-session-id": "session-1",
                "x-client-version": "1.2.3",
                "content-type": "text/plain",
                "x-encrypted": "true",
                "content-length": "3384",
            },
            received_at="2026-06-07T01:02:03",
            decrypted_body_bytes=2048,
        )

        self.assertIn("游戏 Error Log 告警", text)
        self.assertIn("user_id: user-1", text)
        self.assertIn("session_id: session-1", text)
        self.assertIn("NullReferenceException", text)
        self.assertNotIn("secret_debug_dump", text)
        self.assertNotIn("full payload should not be included", text)


if __name__ == "__main__":
    unittest.main()
