import sqlite3
import unittest

from playtime_store import ensure_playtime_schema, record_play_session_event


class PlaytimeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE user_sessions (
                user_id TEXT PRIMARY KEY,
                nickname TEXT,
                player_name TEXT
            )
            """
        )
        ensure_playtime_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _headers(self) -> dict[str, str]:
        return {
            "x-user-id": "user-1",
            "x-session-id": "session-1",
            "x-player-id": "player-1",
            "x-player-session-id": "player-session-1",
            "x-client-version": "unity-test",
            "cf-ipcountry": "CN",
        }

    def test_heartbeat_rollup_tracks_open_session_without_logoff(self) -> None:
        record_play_session_event(
            self.conn,
            payload={"session_id": "session-1", "timestamp": "2026-06-07T01:00:00Z"},
            headers=self._headers(),
            event_type="login",
            received_at="2026-06-07T01:00:00+00:00",
        )
        rollup = record_play_session_event(
            self.conn,
            payload={
                "session_id": "session-1",
                "timestamp": "2026-06-07T01:01:00Z",
                "sequence": 1,
                "game_duration_sec": 60,
                "foreground_duration_sec": 60,
                "active_duration_sec": 45,
                "app_state": "foreground",
                "last_gameplay_event_at": "2026-06-07T01:00:55Z",
            },
            headers=self._headers(),
            event_type="heartbeat",
            received_at="2026-06-07T01:01:00+00:00",
        )

        self.assertEqual(rollup["status"], "open")
        self.assertEqual(rollup["duration_source"], "heartbeat_client")
        self.assertEqual(rollup["confidence"], "high")
        self.assertEqual(rollup["final_duration_sec"], 60)
        self.assertEqual(rollup["heartbeat_count"], 1)
        self.assertEqual(rollup["app_state"], "foreground")

        summary = self.conn.execute("SELECT * FROM playtime_player_summary").fetchone()
        self.assertEqual(summary["session_count"], 1)
        self.assertEqual(summary["heartbeat_session_count"], 1)
        self.assertEqual(summary["total_play_duration_sec"], 60)

    def test_logoff_duration_overrides_heartbeat_duration(self) -> None:
        headers = self._headers()
        record_play_session_event(
            self.conn,
            payload={
                "session_id": "session-1",
                "timestamp": "2026-06-07T01:01:00Z",
                "sequence": 1,
                "game_duration_sec": 60,
            },
            headers=headers,
            event_type="heartbeat",
            received_at="2026-06-07T01:01:00+00:00",
        )
        rollup = record_play_session_event(
            self.conn,
            payload={
                "session_id": "session-1",
                "timestamp": "2026-06-07T01:02:10Z",
                "session_duration_sec": 130,
            },
            headers=headers,
            event_type="logoff",
            received_at="2026-06-07T01:02:10+00:00",
        )

        self.assertEqual(rollup["status"], "closed")
        self.assertEqual(rollup["duration_source"], "logoff")
        self.assertEqual(rollup["confidence"], "high")
        self.assertEqual(rollup["final_duration_sec"], 130)
        self.assertEqual(rollup["logoff_duration_sec"], 130)

    def test_outbox_id_makes_heartbeat_idempotent(self) -> None:
        headers = {**self._headers(), "x-outbox-id": "outbox-1"}
        payload = {
            "session_id": "session-1",
            "timestamp": "2026-06-07T01:01:00Z",
            "sequence": 1,
            "game_duration_sec": 60,
        }

        record_play_session_event(
            self.conn,
            payload=payload,
            headers=headers,
            event_type="heartbeat",
            received_at="2026-06-07T01:01:00+00:00",
        )
        record_play_session_event(
            self.conn,
            payload={**payload, "game_duration_sec": 75},
            headers=headers,
            event_type="heartbeat",
            received_at="2026-06-07T01:01:05+00:00",
        )

        event_count = self.conn.execute("SELECT COUNT(*) FROM play_session_events").fetchone()[0]
        rollup = self.conn.execute("SELECT * FROM play_session_rollups").fetchone()

        self.assertEqual(event_count, 1)
        self.assertEqual(rollup["final_duration_sec"], 75)
        self.assertEqual(rollup["event_count"], 1)


if __name__ == "__main__":
    unittest.main()
