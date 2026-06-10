import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from import_gameplay_telemetry import ensure_schema, import_file, import_sample
from version_utils import release_version_from_client_version


class GameplayTelemetryImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE user_sessions (
                user_id TEXT PRIMARY KEY,
                is_developer INTEGER DEFAULT 0,
                nickname TEXT,
                player_name TEXT
            )
            """
        )
        self.conn.execute(
            "INSERT INTO user_sessions (user_id, is_developer, nickname) VALUES ('user-1', 0, '甜甜')"
        )
        ensure_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_release_version_from_client_version(self) -> None:
        self.assertEqual(
            release_version_from_client_version("unity-taptap-0.1.26.6.1.8d0bdf6d"),
            "taptap-0.1",
        )
        self.assertEqual(
            release_version_from_client_version("unity-taptap-0.12.26.6.1.12345678"),
            "taptap-0.12",
        )
        self.assertEqual(
            release_version_from_client_version("unity-steam-0.123.26.6.1.8d0bdf6d"),
            "steam-0.123",
        )
        self.assertEqual(
            release_version_from_client_version("unity-steam-0.123.26.6.1"),
            "steam-0.123",
        )
        self.assertEqual(
            release_version_from_client_version("unity-26.6.1.f3919b28"),
            "unity-26.6.1.f3919b28",
        )
        self.assertEqual(
            release_version_from_client_version("unity-26.6.1.12345678"),
            "unity-26.6.1.12345678",
        )
        self.assertEqual(release_version_from_client_version("unity-dev"), "unity-dev")

    def test_imports_session_ai_usage_and_call_detail(self) -> None:
        sample = {
            "user_id": "user-1",
            "client_version": "unity-test",
            "gameplay_telemetry": {
                "session_meta": {
                    "session_id": "session-1",
                    "real_time_started_iso": "2026-06-07T01:02:03",
                    "game_day_start": 0,
                    "game_day_end": 2,
                    "ai_token_usage": {
                        "session_input_tokens": 1000,
                        "session_output_tokens": 100,
                        "session_total_tokens": 1100,
                        "session_cached_input_tokens": 200,
                        "session_cache_read_input_tokens": 200,
                        "session_billable_uncached_input_tokens": 800,
                        "session_billable_cached_input_tokens": 200,
                        "archive_total_consumed_tokens": 9000,
                        "input_usd_per_million_tokens": 0.25,
                        "output_usd_per_million_tokens": 1.5,
                        "cached_input_usd_per_million_tokens": 0.025,
                    },
                },
                "days": {
                    "0": {
                        "events": [
                            {
                                "event_type": "cat_agent_request",
                                "payload": {"model": "gemini-test"},
                            },
                            {
                                "event_type": "cat_agent_response",
                                "event_game_minutes": 12,
                                "timestamp": "2026-06-07T01:03:00",
                                "actor": {"agent_id": "cat-1", "agent_name": "小猫"},
                                "payload": {
                                    "model": "gemini-test",
                                    "mode": "plan",
                                    "tag": "daily",
                                    "prompt_profile": {"toolset_version": "v1"},
                                    "request_stats": {
                                        "message_count": 3,
                                        "message_content_chars": 1200,
                                        "message_json_chars": 1800,
                                        "tool_count": 2,
                                        "tool_schema_json_chars": 640,
                                        "prompt_cache_key": "cache-1",
                                    },
                                    "response_stats": {
                                        "prompt_tokens": 120,
                                        "completion_tokens": 30,
                                        "total_tokens": 150,
                                        "cached_tokens": 50,
                                        "cache_read_input_tokens": 50,
                                    },
                                },
                            },
                        ]
                    }
                },
            },
        }

        imported = import_sample(
            self.conn,
            source_file="/tmp/sample.json",
            sample=sample,
            imported_at="2026-06-07T01:10:00+00:00",
        )

        self.assertEqual(imported, 1)
        session = self.conn.execute("SELECT * FROM gameplay_sessions").fetchone()
        self.assertEqual(session["ai_usage_source"], "session_meta.ai_token_usage")
        self.assertEqual(session["ai_request_count"], 1)
        self.assertEqual(session["ai_response_count"], 1)
        self.assertEqual(session["ai_token_record_count"], 1)
        self.assertEqual(session["ai_input_tokens"], 1000)
        self.assertEqual(session["ai_output_tokens"], 100)
        self.assertEqual(session["ai_total_tokens"], 1100)
        self.assertEqual(session["ai_cached_input_tokens"], 200)
        self.assertEqual(session["ai_archive_total_consumed_tokens"], 9000)
        self.assertEqual(session["ai_models"], "gemini-test")
        self.assertAlmostEqual(session["ai_estimated_cost_usd"], 0.000355)

        call = self.conn.execute("SELECT * FROM gameplay_ai_calls").fetchone()
        self.assertEqual(call["model"], "gemini-test")
        self.assertEqual(call["toolset_version"], "v1")
        self.assertEqual(call["prompt_cache_key"], "cache-1")
        self.assertEqual(call["total_tokens"], 150)
        self.assertEqual(call["cached_input_tokens"], 50)
        self.assertAlmostEqual(call["estimated_cost_usd"], 0.00006375)

        user_summary = self.conn.execute(
            "SELECT * FROM gameplay_ai_user_summary WHERE user_id = 'user-1'"
        ).fetchone()
        self.assertEqual(user_summary["ai_total_tokens"], 1100)
        self.assertAlmostEqual(user_summary["ai_estimated_cost_usd"], 0.000355)

    def test_imports_release_version_for_session_detail_tables(self) -> None:
        client_version = "unity-taptap-0.1.26.6.1.8d0bdf6d"
        sample = {
            "user_id": "user-1",
            "client_version": client_version,
            "gameplay_telemetry": {
                "session_meta": {
                    "session_id": "session-release",
                    "real_time_started_iso": "2026-06-07T01:02:03",
                },
                "days": {
                    "0": {
                        "events": [
                            {
                                "event_type": "cat_agent_response",
                                "payload": {
                                    "model": "gemini-test",
                                    "response_stats": {
                                        "prompt_tokens": 10,
                                        "completion_tokens": 2,
                                        "total_tokens": 12,
                                    },
                                },
                            },
                        ]
                    }
                },
            },
        }

        import_sample(
            self.conn,
            source_file="/tmp/release.json",
            sample=sample,
            imported_at="2026-06-07T01:10:00+00:00",
        )

        for table in ("gameplay_sessions", "gameplay_days", "gameplay_events", "gameplay_ai_calls"):
            row = self.conn.execute(f"SELECT client_version, release_version FROM {table}").fetchone()
            self.assertEqual(row["client_version"], client_version)
            self.assertEqual(row["release_version"], "taptap-0.1")

        summary = self.conn.execute(
            "SELECT release_versions, client_versions FROM gameplay_player_summary"
        ).fetchone()
        self.assertEqual(summary["release_versions"], "taptap-0.1")
        self.assertEqual(summary["client_versions"], client_version)

    def test_aggregates_event_ai_usage_without_session_meta(self) -> None:
        sample = {
            "user_id": "user-1",
            "client_version": "unity-test",
            "gameplay_telemetry": {
                "session_meta": {"session_id": "session-2"},
                "days": {
                    "0": {
                        "events": [
                            {
                                "event_type": "cat_agent_response",
                                "payload": {
                                    "model": "gemini-test",
                                    "input_usd_per_million_tokens": 0.25,
                                    "output_usd_per_million_tokens": 1.5,
                                    "cached_input_usd_per_million_tokens": 0.025,
                                    "response_stats": {
                                        "prompt_tokens": 100,
                                        "completion_tokens": 20,
                                        "total_tokens": 120,
                                        "cached_tokens": 40,
                                    },
                                },
                            },
                            {
                                "event_type": "cat_agent_response",
                                "payload": {
                                    "model": "gemini-test",
                                    "input_usd_per_million_tokens": 0.25,
                                    "output_usd_per_million_tokens": 1.5,
                                    "cached_input_usd_per_million_tokens": 0.025,
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

        import_sample(
            self.conn,
            source_file="/tmp/sample-2.json",
            sample=sample,
            imported_at="2026-06-07T01:10:00+00:00",
        )

        session = self.conn.execute(
            "SELECT * FROM gameplay_sessions WHERE session_id = 'session-2'"
        ).fetchone()
        self.assertEqual(session["ai_usage_source"], "event_payloads")
        self.assertEqual(session["ai_response_count"], 2)
        self.assertEqual(session["ai_token_record_count"], 2)
        self.assertEqual(session["ai_input_tokens"], 300)
        self.assertEqual(session["ai_output_tokens"], 70)
        self.assertEqual(session["ai_total_tokens"], 370)
        self.assertEqual(session["ai_cached_input_tokens"], 120)
        self.assertAlmostEqual(session["ai_cache_hit_ratio"], 0.4)
        self.assertAlmostEqual(session["ai_estimated_cost_usd"], 0.000153)

    def test_reimport_replaces_rows_by_player_session_id_across_sources(self) -> None:
        first_sample = {
            "user_id": "user-1",
            "player_session_id": "player-session-1",
            "client_version": "unity-test",
            "gameplay_telemetry": {
                "session_meta": {
                    "session_id": "api-session-1",
                    "real_time_started_iso": "2026-06-07T01:00:00",
                    "money_end": 100,
                },
                "days": {
                    "1": {
                        "events": [
                            {"event_type": "old_event", "payload": {"value": 1}},
                        ]
                    }
                },
            },
        }
        second_sample = {
            "user_id": "user-1",
            "player_session_id": "player-session-1",
            "client_version": "unity-test",
            "gameplay_telemetry": {
                "session_meta": {
                    "session_id": "api-session-2",
                    "real_time_started_iso": "2026-06-07T01:00:00",
                    "money_end": 200,
                },
                "days": {
                    "2": {
                        "events": [
                            {"event_type": "new_event", "payload": {"value": 2}},
                        ]
                    }
                },
            },
        }

        import_sample(
            self.conn,
            source_file="/tmp/outbox-old.json",
            sample=first_sample,
            imported_at="2026-06-07T01:10:00+00:00",
        )
        import_sample(
            self.conn,
            source_file="/tmp/outbox-retry.json",
            sample=second_sample,
            imported_at="2026-06-07T01:20:00+00:00",
        )

        sessions = self.conn.execute("SELECT * FROM gameplay_sessions").fetchall()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["source_file"], "/tmp/outbox-retry.json")
        self.assertEqual(sessions[0]["session_id"], "api-session-2")
        self.assertEqual(sessions[0]["player_session_id"], "player-session-1")
        self.assertEqual(sessions[0]["money_end"], 200)

        day = self.conn.execute("SELECT * FROM gameplay_days").fetchone()
        self.assertEqual(day["session_id"], "api-session-2")
        self.assertEqual(day["player_session_id"], "player-session-1")
        self.assertEqual(day["game_day"], 2)

        event = self.conn.execute("SELECT * FROM gameplay_events").fetchone()
        self.assertEqual(event["session_id"], "api-session-2")
        self.assertEqual(event["player_session_id"], "player-session-1")
        self.assertEqual(event["event_type"], "new_event")

    def test_import_file_tracks_raw_ingest_status_and_session(self) -> None:
        payload = {
            "ingest": {
                "type": "gameplay_telemetry_ingest",
                "id": "ingest-1",
                "endpoint": "/logoff",
                "event_type": "logoff",
                "received_at": "2026-06-07T01:02:03",
                "user_id": "user-1",
                "session_id": "session-1",
                "player_session_id": "player-session-1",
                "player_id": "player-1",
                "client_version": "unity-test",
                "outbox_id": "outbox-1",
                "payload_size_bytes": 1234,
                "import_status": "pending",
            },
            "user_id": "user-1",
            "player_id": "player-1",
            "session_id": "session-1",
            "player_session_id": "player-session-1",
            "client_version": "unity-test",
            "gameplay_telemetry": {
                "session_meta": {
                    "session_id": "session-1",
                    "player_session_id": "player-session-1",
                    "real_time_started_iso": "2026-06-07T01:00:00",
                    "game_duration_sec": 60,
                    "money_end": 200,
                },
                "days": {"1": {"events": [{"event_type": "fish", "payload": {"earned_money": 20}}]}},
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "raw-ingest.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            source_file = str(path.resolve())

            session_count, sample_count = import_file(self.conn, path)

        self.assertEqual(session_count, 1)
        self.assertEqual(sample_count, 1)
        ingest = self.conn.execute(
            "SELECT * FROM gameplay_telemetry_ingest WHERE source_file = ?",
            (source_file,),
        ).fetchone()
        self.assertEqual(ingest["import_status"], "imported")
        self.assertEqual(ingest["sample_count"], 1)
        self.assertEqual(ingest["session_count"], 1)
        self.assertEqual(ingest["player_session_id"], "player-session-1")
        self.assertEqual(ingest["outbox_id"], "outbox-1")
        self.assertEqual(ingest["release_version"], "unity-test")

        session = self.conn.execute("SELECT * FROM gameplay_sessions").fetchone()
        self.assertEqual(session["source_file"], source_file)
        self.assertEqual(session["session_id"], "session-1")
        self.assertEqual(session["player_session_id"], "player-session-1")
        self.assertEqual(session["client_version"], "unity-test")
        self.assertEqual(session["money_end"], 200)

    def test_import_file_marks_raw_ingest_without_gameplay_as_skipped(self) -> None:
        payload = {
            "ingest": {
                "type": "gameplay_telemetry_ingest",
                "id": "ingest-empty",
                "endpoint": "/logoff",
                "event_type": "logoff",
                "user_id": "user-1",
                "session_id": "session-1",
            },
            "user_id": "user-1",
            "session_id": "session-1",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "raw-ingest-empty.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            source_file = str(path.resolve())

            session_count, sample_count = import_file(self.conn, path)

        self.assertEqual(session_count, 0)
        self.assertEqual(sample_count, 0)
        ingest = self.conn.execute(
            "SELECT * FROM gameplay_telemetry_ingest WHERE source_file = ?",
            (source_file,),
        ).fetchone()
        self.assertEqual(ingest["import_status"], "skipped")
        self.assertIn("no gameplay telemetry", ingest["import_error"])


if __name__ == "__main__":
    unittest.main()
