import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.feishu_alerts import (
    _build_error_log_alert_text,
    _build_logoff_report_document_text,
    _build_logoff_report_summary,
    _doc_url,
    _infer_member_type,
    _is_unity_dev_payload,
    send_error_log_alert,
    send_logoff_telemetry_report,
)


class GameTelemetryEncryptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Fernet.generate_key().decode("utf-8")
        self.fernet = Fernet(self.key.encode("utf-8"))
        self.env_patch = patch.dict(os.environ, {"PAW_FERNET_KEY": self.key, "DB_PATH": ""})
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
            "X-Player-ID": "player-1",
            "X-Player-Session-ID": "player-session-1",
            "X-Client-Version": "1.2.3",
        }

    def test_encrypted_login_reaches_session_parser(self) -> None:
        captured = {}
        payload = {"session_id": "session-1", "timestamp": "2026-06-07T01:02:03Z"}

        def fake_create_task(value):
            return value

        async def fake_update_session_event_log(**kwargs):
            captured["event_type"] = kwargs["event_type"]
            captured["payload"] = json.loads(kwargs["raw_body"])
            return object()

        async def fake_save_play_session_event(**kwargs):
            captured["play_event_type"] = kwargs["event_type"]
            captured["play_payload"] = kwargs["payload"]
            return {"duration_source": "login_only", "final_duration_sec": 0}

        with (
            patch("app.api.routes.system.asyncio.create_task", fake_create_task),
            patch(
                "app.api.routes.system.sessions.update_session_event_log",
                fake_update_session_event_log,
            ),
            patch(
                "app.api.routes.system.sessions.save_play_session_event",
                fake_save_play_session_event,
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
        self.assertEqual(captured["play_event_type"], "login")
        self.assertEqual(captured["play_payload"], payload)

    def test_encrypted_session_heartbeat_persists_playtime(self) -> None:
        captured = {}
        payload = {
            "session_id": "session-1",
            "player_session_id": "player-session-1",
            "timestamp": "2026-06-07T01:03:00Z",
            "sequence": 3,
            "game_duration_sec": 90,
            "foreground_duration_sec": 90,
            "active_duration_sec": 60,
            "app_state": "foreground",
        }

        async def fake_update_session_event_log(**kwargs):
            captured["event_type"] = kwargs["event_type"]
            captured["payload"] = json.loads(kwargs["raw_body"])
            return object()

        async def fake_save_play_session_event(**kwargs):
            captured["play_event_type"] = kwargs["event_type"]
            captured["play_payload"] = kwargs["payload"]
            return {
                "duration_source": "heartbeat_client",
                "final_duration_sec": 90,
                "status": "open",
                "confidence": "high",
            }

        with (
            patch(
                "app.api.routes.system.sessions.update_session_event_log",
                fake_update_session_event_log,
            ),
            patch(
                "app.api.routes.system.sessions.save_play_session_event",
                fake_save_play_session_event,
            ),
        ):
            response = self.client.post(
                "/session_heartbeat",
                content=self._encrypt(payload),
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["event_type"], "heartbeat")
        self.assertEqual(captured["payload"], payload)
        self.assertEqual(captured["play_event_type"], "heartbeat")
        self.assertEqual(response.json()["duration_source"], "heartbeat_client")
        self.assertEqual(response.json()["final_duration_sec"], 90)
        self.assertEqual(response.json()["session_status"], "open")
        self.assertEqual(response.json()["confidence"], "high")

    def test_v1_encrypted_session_heartbeat_persists_activity_to_sqlite(self) -> None:
        payload = {
            "session_id": "session-1",
            "player_session_id": "player-session-1",
            "timestamp": "2026-06-07T01:03:00Z",
            "sequence": 3,
            "game_duration_sec": 90,
            "foreground_duration_sec": 90,
            "active_duration_sec": 60,
            "app_state": "foreground",
            "activity_state": "active",
            "current_activity": "fishing",
            "current_ui": "none",
            "idle_duration_sec": 12,
            "afk_duration_sec": 0,
            "input_active_duration_sec": 35,
            "movement_duration_sec": 8,
            "gameplay_active_duration_sec": 30,
            "ui_active_duration_sec": 3,
            "last_input_at": "2026-06-07T01:02:59Z",
            "last_player_action_at": "2026-06-07T01:02:58Z",
            "last_movement_at": "2026-06-07T01:02:30Z",
            "activity_window_sec": 30,
            "activity_since_last_heartbeat": {
                "input_event_count": 4,
                "movement_start_count": 1,
                "gameplay_event_count": 2,
                "interact_count": 1,
                "fishing_action_count": 2,
                "ui_open_count": 1,
                "ui_click_count": 1,
            },
            "activity_thresholds": {
                "idle_threshold_sec": 15,
                "afk_threshold_sec": 120,
            },
        }

        async def fake_update_session_event_log(**kwargs):
            return object()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "telemetry.db")
            with (
                patch.dict(os.environ, {"DB_PATH": db_path}),
                patch(
                    "app.api.routes.system.sessions.update_session_event_log",
                    fake_update_session_event_log,
                ),
            ):
                response = self.client.post(
                    "/v1/session_heartbeat",
                    content=self._encrypt(payload),
                    headers={**self._headers(), "X-Outbox-ID": "outbox-heartbeat-1"},
                )

            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                event = conn.execute("SELECT * FROM play_session_events").fetchone()
                rollup = conn.execute("SELECT * FROM play_session_rollups").fetchone()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["duration_source"], "heartbeat_client")
        self.assertEqual(event["event_type"], "heartbeat")
        self.assertEqual(event["activity_state"], "active")
        self.assertEqual(event["current_activity"], "fishing")
        self.assertEqual(event["activity_fishing_action_count"], 2)
        self.assertEqual(rollup["final_duration_sec"], 90)
        self.assertEqual(rollup["activity_state"], "active")
        self.assertEqual(rollup["current_activity"], "fishing")
        self.assertEqual(rollup["idle_duration_sec"], 12)
        self.assertEqual(rollup["input_active_duration_sec"], 35)
        self.assertEqual(rollup["gameplay_active_duration_sec"], 30)
        self.assertEqual(rollup["activity_reported_window_sec"], 30)
        self.assertEqual(rollup["activity_input_event_count"], 4)
        self.assertEqual(rollup["activity_fishing_action_count"], 2)

    def test_encrypted_logoff_preserves_root_gameplay_telemetry(self) -> None:
        captured = {}
        reports = {}
        payload = {
            "session_id": "session-1",
            "timestamp": "2026-06-07T01:02:03Z",
            "session_duration_sec": 125,
            "total_money": 1318,
            "tasks_completed": 8,
            "tasks_total": 9,
            "gameplay_telemetry": {
                "session_meta": {
                    "session_id": "session-1",
                    "game_duration_sec": 125,
                    "game_days_total": 2,
                    "money_start": 2000,
                    "money_end": 1318,
                    "money_net_delta": -682,
                },
                "days": {
                    "1": {"events": [{"type": "move"}, {"type": "fish"}]},
                },
            },
        }

        def fake_create_task(value):
            return value

        async def fake_update_session_event_log(**kwargs):
            captured["event_type"] = kwargs["event_type"]
            captured["payload"] = json.loads(kwargs["raw_body"])
            return object()

        async def fake_save_gameplay_telemetry_ingest(**kwargs):
            captured["ingest_payload"] = kwargs["payload"]
            captured["ingest_headers"] = kwargs["headers"]
            return object()

        async def fake_save_play_session_event(**kwargs):
            captured["play_event_type"] = kwargs["event_type"]
            captured["play_payload"] = kwargs["payload"]
            return {"duration_source": "logoff", "final_duration_sec": 125}

        def fake_send_logoff_telemetry_report(**kwargs):
            reports.update(kwargs)
            return object()

        with (
            patch("app.api.routes.system.asyncio.create_task", fake_create_task),
            patch(
                "app.api.routes.system.sessions.update_session_event_log",
                fake_update_session_event_log,
            ),
            patch(
                "app.api.routes.system.sessions.save_gameplay_telemetry_ingest",
                fake_save_gameplay_telemetry_ingest,
            ),
            patch(
                "app.api.routes.system.sessions.save_play_session_event",
                fake_save_play_session_event,
            ),
            patch(
                "app.api.routes.system.feishu_alerts.send_logoff_telemetry_report",
                fake_send_logoff_telemetry_report,
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
        self.assertEqual(captured["play_event_type"], "logoff")
        self.assertEqual(captured["play_payload"]["gameplay_telemetry"], payload["gameplay_telemetry"])
        self.assertEqual(captured["ingest_payload"]["gameplay_telemetry"], payload["gameplay_telemetry"])
        self.assertEqual(captured["ingest_headers"]["x-player-session-id"], "player-session-1")
        self.assertEqual(reports["payload"]["gameplay_telemetry"], payload["gameplay_telemetry"])
        self.assertEqual(reports["headers"]["x-session-id"], "session-1")

    def test_encrypted_logoff_writes_raw_ingest_file_before_success(self) -> None:
        payload = {
            "session_id": "session-1",
            "timestamp": "2026-06-07T01:02:03Z",
            "username": "甜甜",
            "gameplay_telemetry": {
                "session_meta": {
                    "session_id": "session-1",
                    "player_session_id": "player-session-1",
                    "client_version": "1.2.3",
                    "game_duration_sec": 125,
                },
                "days": {"1": {"events": [{"event_type": "fish"}]}},
            },
        }

        def fake_create_task(value):
            return value

        async def fake_update_session_event_log(**kwargs):
            return object()

        def fake_send_logoff_telemetry_report(**kwargs):
            return object()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch("app.api.routes.system.asyncio.create_task", fake_create_task),
                patch(
                    "app.api.routes.system.sessions.update_session_event_log",
                    fake_update_session_event_log,
                ),
                patch("app.services.sessions.GAMEPLAY_TELEMETRY_DIR", Path(tmpdir)),
                patch(
                    "app.api.routes.system.feishu_alerts.send_logoff_telemetry_report",
                    fake_send_logoff_telemetry_report,
                ),
            ):
                response = self.client.post(
                    "/logoff",
                    content=self._encrypt(payload),
                    headers=self._headers(),
                )

            files = list(Path(tmpdir).rglob("*.json"))
            record = json.loads(files[0].read_text(encoding="utf-8")) if files else {}

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(files), 1)
        self.assertEqual(record["ingest"]["type"], "gameplay_telemetry_ingest")
        self.assertEqual(record["ingest"]["event_type"], "logoff")
        self.assertEqual(record["ingest"]["import_status"], "pending")
        self.assertEqual(record["ingest"]["user_id"], "user-1")
        self.assertEqual(record["ingest"]["player_id"], "player-1")
        self.assertEqual(record["ingest"]["player_session_id"], "player-session-1")
        self.assertEqual(record["user_id"], "user-1")
        self.assertEqual(record["client_version"], "1.2.3")
        self.assertEqual(record["gameplay_telemetry"], payload["gameplay_telemetry"])

    def test_encrypted_logoff_returns_503_when_persist_fails(self) -> None:
        payload = {"session_id": "session-1", "timestamp": "2026-06-07T01:02:03Z"}

        async def fake_update_session_event_log(**kwargs):
            raise OSError("disk full")

        with patch(
            "app.api.routes.system.sessions.update_session_event_log",
            fake_update_session_event_log,
        ):
            response = self.client.post(
                "/logoff",
                content=self._encrypt(payload),
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "failed to persist logoff telemetry")

    def test_encrypted_logoff_returns_503_when_raw_ingest_fails(self) -> None:
        payload = {
            "session_id": "session-1",
            "timestamp": "2026-06-07T01:02:03Z",
            "gameplay_telemetry": {"session_meta": {"session_id": "session-1"}, "days": {}},
        }

        async def fake_update_session_event_log(**kwargs):
            return object()

        async def fake_save_gameplay_telemetry_ingest(**kwargs):
            raise OSError("disk full")

        with (
            patch(
                "app.api.routes.system.sessions.update_session_event_log",
                fake_update_session_event_log,
            ),
            patch(
                "app.api.routes.system.sessions.save_gameplay_telemetry_ingest",
                fake_save_gameplay_telemetry_ingest,
            ),
        ):
            response = self.client.post(
                "/logoff",
                content=self._encrypt(payload),
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "failed to persist logoff telemetry")

    def test_encrypted_heartbeat_returns_503_when_persist_fails(self) -> None:
        payload = {"session_id": "session-1", "sequence": 1, "game_duration_sec": 30}

        async def fake_update_session_event_log(**kwargs):
            return object()

        async def fake_save_play_session_event(**kwargs):
            raise OSError("disk full")

        with (
            patch(
                "app.api.routes.system.sessions.update_session_event_log",
                fake_update_session_event_log,
            ),
            patch(
                "app.api.routes.system.sessions.save_play_session_event",
                fake_save_play_session_event,
            ),
        ):
            response = self.client.post(
                "/session_heartbeat",
                content=self._encrypt(payload),
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "failed to persist session heartbeat")

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

    def test_unity_dev_detection_checks_all_client_version_sources(self) -> None:
        self.assertTrue(_is_unity_dev_payload({}, {"X-Client-Version": "UNITY-DEV"}))
        self.assertTrue(_is_unity_dev_payload({"client_version": "unity-dev"}, {}))
        self.assertTrue(
            _is_unity_dev_payload(
                {
                    "gameplay_telemetry": {
                        "session_meta": {
                            "client_version": "unity-dev",
                        }
                    }
                },
                {"x-client-version": "1.2.3"},
            )
        )
        self.assertFalse(
            _is_unity_dev_payload(
                {"client_version": "unity-test"},
                {"x-client-version": "1.2.3"},
            )
        )

    def test_unity_dev_feishu_senders_do_not_open_http_client(self) -> None:
        env = {
            "FEISHU_BOT_API_KEY": "app-id",
            "FEISHU_BOT_API_SECRET": "app-secret",
            "FEISHU_CHAT_ID": "chat-id",
            "FEISHU_ADMIN_ID": "ou_admin",
            "FEISHU_ERROR_LOG_ALERTS": "true",
            "FEISHU_LOGOFF_REPORTS": "true",
        }
        payload = {
            "user_id": "user-1",
            "session_id": "session-1",
            "message": "NullReferenceException",
            "gameplay_telemetry": {
                "session_meta": {
                    "client_version": "unity-dev",
                    "session_id": "session-1",
                },
                "days": {},
            },
        }
        headers = {
            "x-user-id": "user-1",
            "x-session-id": "session-1",
            "x-client-version": "1.2.3",
        }

        with (
            patch.dict(os.environ, env),
            patch("app.services.feishu_alerts.httpx.AsyncClient") as async_client,
        ):
            asyncio.run(
                send_error_log_alert(
                    payload=payload,
                    headers=headers,
                    received_at="2026-06-07T01:02:03",
                    decrypted_body_bytes=2048,
                )
            )
            asyncio.run(
                send_logoff_telemetry_report(
                    payload=payload,
                    headers=headers,
                    received_at="2026-06-07T01:02:03",
                )
            )

        async_client.assert_not_called()

    def test_logoff_report_summary_and_document_text(self) -> None:
        payload = {
            "user_id": "user-1",
            "username": "甜甜",
            "session_id": "session-1",
            "client_version": "unity-test",
            "total_money": 1318,
            "tasks_completed": 8,
            "tasks_total": 9,
            "gameplay_telemetry": {
                "session_meta": {
                    "game_duration_sec": 1162.99,
                    "game_day_start": 0,
                    "game_day_end": 2,
                    "game_days_total": 2,
                    "money_start": 2000,
                    "money_end": 1318,
                    "money_total_earned": 391,
                    "money_total_spent": 1008,
                    "money_net_delta": -617,
                    "money_reconciliation_ok": False,
                    "runtime_environment": {
                        "platform": "WindowsPlayer",
                        "application_version": "26.6.1",
                        "unity_version": "6000.3.14f1",
                    },
                    "outbox": {
                        "pending_count": 0,
                        "last_flush_success_count": 1,
                        "last_flush_failure_count": 0,
                    },
                    "ai_token_usage": {
                        "archive_total_consumed_tokens": 1900472,
                        "session_input_tokens": 182436,
                        "session_output_tokens": 4187,
                        "session_total_tokens": 186623,
                        "session_cached_input_tokens": 43630,
                        "session_cache_read_input_tokens": 43630,
                        "session_cache_creation_input_tokens": 0,
                        "session_billable_uncached_input_tokens": 138806,
                        "session_billable_cached_input_tokens": 43630,
                        "input_usd_per_million_tokens": 0.25,
                        "output_usd_per_million_tokens": 1.5,
                        "cached_input_usd_per_million_tokens": 0.025,
                    },
                },
                "days": {
                    "0": {
                        "events": [
                            {"event_type": "cat_agent_request", "payload": {"model": "gemini-test"}},
                            {"event_type": "cat_agent_response", "payload": {"model": "gemini-test"}},
                        ]
                    },
                    "1": {"events": [{"type": "c"}]},
                },
            },
        }
        headers = self._headers()

        summary = _build_logoff_report_summary(
            payload=payload,
            headers=headers,
            received_at="2026-06-07T01:02:03",
            document_url="https://www.feishu.cn/docx/doc123",
        )
        document_text = _build_logoff_report_document_text(
            payload=payload,
            headers=headers,
            received_at="2026-06-07T01:02:03",
        )

        self.assertIn("游戏 Logoff Telemetry Report", summary)
        self.assertIn("user_id: user-1", summary)
        self.assertIn("telemetry_events: 3", summary)
        self.assertIn("game_day_range: 0 -> 2", summary)
        self.assertIn("ai_events: request=1 response=1 token_records=0", summary)
        self.assertIn("ai_tokens: input=182,436 output=4,187 total=186,623 cached_input=43,630", summary)
        self.assertIn("ai_cost_est_usd: $0.04", summary)
        self.assertIn("user_total_sessions: 1 (source=current_logoff)", summary)
        self.assertIn("user_total_ai_tokens: input=182,436 output=4,187 total=186,623 cached_input=43,630", summary)
        self.assertIn("money_flow: earned=391 spent=1008 reconciliation_ok=False", summary)
        self.assertIn("full_report_doc: https://www.feishu.cn/docx/doc123", summary)
        self.assertIn("AI Token Usage", document_text)
        self.assertIn("User Aggregate", document_text)
        self.assertIn("Derived Metrics", document_text)
        self.assertIn('"ai_token_usage"', document_text)
        self.assertIn('"user_aggregate"', document_text)
        self.assertIn("Full JSON", document_text)
        self.assertIn('"gameplay_telemetry"', document_text)
        self.assertIn('"money_start": 2000', document_text)

    def test_logoff_report_adds_user_aggregate_from_gameplay_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "conversations.db")
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE gameplay_sessions (
                    user_id TEXT,
                    session_id TEXT,
                    game_duration_sec REAL,
                    ai_request_count INTEGER,
                    ai_response_count INTEGER,
                    ai_token_record_count INTEGER,
                    ai_input_tokens INTEGER,
                    ai_output_tokens INTEGER,
                    ai_total_tokens INTEGER,
                    ai_cached_input_tokens INTEGER,
                    ai_estimated_cost_usd REAL,
                    game_days_total INTEGER,
                    game_day_end INTEGER,
                    island_level_max INTEGER,
                    real_time_started_iso TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO gameplay_sessions VALUES (
                    'user-1', 'session-old', 3600, 2, 2, 1,
                    1000, 100, 1100, 200, 0.02,
                    3, 3, 4, '2026-06-01T01:00:00'
                )
                """
            )
            conn.commit()
            conn.close()

            payload = {
                "user_id": "user-1",
                "session_id": "session-current",
                "gameplay_telemetry": {
                    "session_meta": {
                        "session_id": "session-current",
                        "game_duration_sec": 60,
                        "game_day_end": 2,
                        "game_days_total": 2,
                        "island_level_max": 2,
                        "real_time_started_iso": "2026-06-07T01:00:00",
                        "ai_token_usage": {
                            "session_input_tokens": 100000,
                            "session_output_tokens": 10000,
                            "session_total_tokens": 110000,
                            "session_cached_input_tokens": 40000,
                            "session_billable_uncached_input_tokens": 60000,
                            "session_billable_cached_input_tokens": 40000,
                            "input_usd_per_million_tokens": 0.25,
                            "output_usd_per_million_tokens": 1.5,
                            "cached_input_usd_per_million_tokens": 0.025,
                        },
                    },
                    "days": {
                        "0": {
                            "events": [
                                {"event_type": "cat_agent_request", "payload": {"model": "gemini-test"}},
                                {
                                    "event_type": "cat_agent_response",
                                    "payload": {
                                        "model": "gemini-test",
                                        "response_stats": {
                                            "prompt_tokens": 100000,
                                            "completion_tokens": 10000,
                                            "total_tokens": 110000,
                                            "cached_tokens": 40000,
                                        },
                                    },
                                },
                            ]
                        }
                    },
                },
            }

            with patch.dict(os.environ, {"DB_PATH": db_path}):
                summary = _build_logoff_report_summary(
                    payload=payload,
                    headers=self._headers(),
                    received_at="2026-06-07T01:02:03",
                )

        self.assertIn("user_total_sessions: 2 (source=db+current_logoff)", summary)
        self.assertIn("user_total_playtime: 1h 1m 0s (3,660s)", summary)
        self.assertIn(
            "user_total_ai_tokens: input=101,000 output=10,100 total=111,100 cached_input=40,200",
            summary,
        )
        self.assertIn("user_total_ai_cost_est_usd: $0.05", summary)
        self.assertIn("user_total_ai_calls: request=3 response=3 token_records=2", summary)
        self.assertIn("user_ai_cache_hit: 39.8%", summary)
        self.assertIn("user_progress_max: game_days=3 island_level=4", summary)
        self.assertIn("user_session_window: 2026-06-01T01:00:00 -> 2026-06-07T01:00:00", summary)

    def test_logoff_report_does_not_double_count_current_session_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "conversations.db")
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE gameplay_sessions (
                    user_id TEXT,
                    session_id TEXT,
                    game_duration_sec REAL,
                    ai_request_count INTEGER,
                    ai_response_count INTEGER,
                    ai_token_record_count INTEGER,
                    ai_input_tokens INTEGER,
                    ai_output_tokens INTEGER,
                    ai_total_tokens INTEGER,
                    ai_cached_input_tokens INTEGER,
                    ai_estimated_cost_usd REAL,
                    game_days_total INTEGER,
                    game_day_end INTEGER,
                    island_level_max INTEGER,
                    real_time_started_iso TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO gameplay_sessions VALUES (
                    'user-1', 'session-1', 60, 1, 1, 1,
                    100, 20, 120, 40, 0.01,
                    2, 2, 2, '2026-06-07T01:00:00'
                )
                """
            )
            conn.commit()
            conn.close()

            payload = {
                "user_id": "user-1",
                "session_id": "session-1",
                "gameplay_telemetry": {
                    "session_meta": {
                        "session_id": "session-1",
                        "game_duration_sec": 60,
                        "game_days_total": 2,
                        "ai_token_usage": {
                            "session_input_tokens": 100,
                            "session_output_tokens": 20,
                            "session_total_tokens": 120,
                            "session_cached_input_tokens": 40,
                        },
                    },
                    "days": {},
                },
            }

            with patch.dict(os.environ, {"DB_PATH": db_path}):
                summary = _build_logoff_report_summary(
                    payload=payload,
                    headers=self._headers(),
                    received_at="2026-06-07T01:02:03",
                )

        self.assertIn("user_total_sessions: 1 (source=db)", summary)
        self.assertIn("user_total_ai_tokens: input=100 output=20 total=120 cached_input=40", summary)

    def test_logoff_report_aggregates_event_token_usage_without_session_meta(self) -> None:
        payload = {
            "user_id": "user-1",
            "session_id": "session-1",
            "gameplay_telemetry": {
                "session_meta": {"game_duration_sec": 30},
                "days": {
                    "0": {
                        "events": [
                            {
                                "event_type": "cat_agent_response",
                                "payload": {
                                    "model": "gemini-test",
                                    "input_tokens": 100,
                                    "output_tokens": 25,
                                    "total_tokens": 125,
                                    "cached_tokens": 40,
                                    "cache_read_input_tokens": 40,
                                    "response_stats": {
                                        "prompt_tokens": 100,
                                        "completion_tokens": 25,
                                        "total_tokens": 125,
                                        "cached_tokens": 40,
                                        "cache_read_input_tokens": 40,
                                    },
                                },
                            },
                            {
                                "event_type": "cat_agent_response",
                                "payload": {
                                    "model": "gemini-test",
                                    "response_stats": {
                                        "prompt_tokens": 200,
                                        "completion_tokens": 50,
                                        "total_tokens": 250,
                                        "cached_tokens": 80,
                                    },
                                },
                            },
                        ]
                    }
                },
            },
        }

        summary = _build_logoff_report_summary(
            payload=payload,
            headers=self._headers(),
            received_at="2026-06-07T01:02:03",
        )
        document_text = _build_logoff_report_document_text(
            payload=payload,
            headers=self._headers(),
            received_at="2026-06-07T01:02:03",
        )

        self.assertIn("ai_events: request=0 response=2 token_records=2", summary)
        self.assertIn("ai_tokens: input=300 output=75 total=375 cached_input=120", summary)
        self.assertIn("ai_billable_input: uncached=180 cached=120", summary)
        self.assertIn('"source": "event_payloads"', document_text)

    def test_infer_admin_member_type_from_oc_prefix(self) -> None:
        with patch.dict(os.environ, {"FEISHU_ADMIN_MEMBER_TYPE": ""}):
            self.assertEqual(_infer_member_type("oc_1fcd2d616f2899538f85af4a0e16cf48"), "openchat")
            self.assertEqual(_infer_member_type("ou_xxx"), "openid")

    def test_doc_url_adds_docx_path_for_tenant_base_url(self) -> None:
        with patch.dict(os.environ, {"FEISHU_DOC_BASE_URL": "https://meowjito.feishu.cn/"}):
            self.assertEqual(
                _doc_url("DbF7duwowoah0BxzzwDchngQnPh"),
                "https://meowjito.feishu.cn/docx/DbF7duwowoah0BxzzwDchngQnPh",
            )

        with patch.dict(os.environ, {"FEISHU_DOC_BASE_URL": "https://meowjito.feishu.cn/docx"}):
            self.assertEqual(
                _doc_url("DbF7duwowoah0BxzzwDchngQnPh"),
                "https://meowjito.feishu.cn/docx/DbF7duwowoah0BxzzwDchngQnPh",
            )


if __name__ == "__main__":
    unittest.main()
