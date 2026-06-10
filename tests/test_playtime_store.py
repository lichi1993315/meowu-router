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

    def test_heartbeat_activity_fields_roll_up_and_outbox_update_is_idempotent(self) -> None:
        headers_1 = {**self._headers(), "x-outbox-id": "outbox-1"}
        headers_2 = {**self._headers(), "x-outbox-id": "outbox-2"}
        heartbeat_1 = {
            "session_id": "session-1",
            "timestamp": "2026-06-07T01:01:00Z",
            "sequence": 1,
            "game_duration_sec": 60,
            "activity_state": "active",
            "current_activity": "moving",
            "current_ui": "none",
            "idle_duration_sec": 10,
            "afk_duration_sec": 0,
            "input_active_duration_sec": 20,
            "movement_duration_sec": 15,
            "gameplay_active_duration_sec": 12,
            "ui_active_duration_sec": 1,
            "last_input_at": "2026-06-07T01:00:59Z",
            "last_player_action_at": "2026-06-07T01:00:58Z",
            "last_movement_at": "2026-06-07T01:00:57Z",
            "activity_window_sec": 30,
            "activity_since_last_heartbeat": {
                "input_event_count": 5,
                "movement_start_count": 1,
                "gameplay_event_count": 2,
                "interact_count": 1,
                "fishing_action_count": 0,
                "ui_open_count": 1,
                "ui_click_count": 2,
            },
            "activity_thresholds": {
                "idle_threshold_sec": 15,
                "afk_threshold_sec": 120,
            },
        }
        heartbeat_2 = {
            **heartbeat_1,
            "timestamp": "2026-06-07T01:01:30Z",
            "sequence": 2,
            "game_duration_sec": 90,
            "activity_state": "afk",
            "current_activity": "afk",
            "idle_duration_sec": 35,
            "afk_duration_sec": 5,
            "input_active_duration_sec": 20,
            "movement_duration_sec": 15,
            "gameplay_active_duration_sec": 12,
            "ui_active_duration_sec": 1,
            "activity_since_last_heartbeat": {
                "input_event_count": 1,
                "movement_start_count": 0,
                "gameplay_event_count": 1,
                "interact_count": 0,
                "fishing_action_count": 1,
                "ui_open_count": 0,
                "ui_click_count": 0,
            },
        }

        record_play_session_event(
            self.conn,
            payload=heartbeat_1,
            headers=headers_1,
            event_type="heartbeat",
            received_at="2026-06-07T01:01:00+00:00",
        )
        # Same outbox record with a changed payload updates the event row instead of double-counting it.
        heartbeat_1_retry = {
            **heartbeat_1,
            "activity_since_last_heartbeat": {
                **heartbeat_1["activity_since_last_heartbeat"],
                "input_event_count": 7,
            },
        }
        record_play_session_event(
            self.conn,
            payload=heartbeat_1_retry,
            headers=headers_1,
            event_type="heartbeat",
            received_at="2026-06-07T01:01:05+00:00",
        )
        rollup = record_play_session_event(
            self.conn,
            payload=heartbeat_2,
            headers=headers_2,
            event_type="heartbeat",
            received_at="2026-06-07T01:01:30+00:00",
        )

        event_count = self.conn.execute("SELECT COUNT(*) FROM play_session_events").fetchone()[0]
        first_event = self.conn.execute(
            "SELECT * FROM play_session_events WHERE outbox_id = 'outbox-1'"
        ).fetchone()

        self.assertEqual(event_count, 2)
        self.assertEqual(first_event["activity_input_event_count"], 7)
        self.assertEqual(rollup["activity_state"], "afk")
        self.assertEqual(rollup["current_activity"], "afk")
        self.assertEqual(rollup["idle_duration_sec"], 35)
        self.assertEqual(rollup["afk_duration_sec"], 5)
        self.assertEqual(rollup["input_active_duration_sec"], 20)
        self.assertEqual(rollup["movement_duration_sec"], 15)
        self.assertEqual(rollup["gameplay_active_duration_sec"], 12)
        self.assertEqual(rollup["ui_active_duration_sec"], 1)
        self.assertEqual(rollup["last_input_at"], "2026-06-07T01:00:59Z")
        self.assertEqual(rollup["activity_reported_window_sec"], 60)
        self.assertEqual(rollup["activity_input_event_count"], 8)
        self.assertEqual(rollup["activity_movement_start_count"], 1)
        self.assertEqual(rollup["activity_gameplay_event_count"], 3)
        self.assertEqual(rollup["activity_interact_count"], 1)
        self.assertEqual(rollup["activity_fishing_action_count"], 1)
        self.assertEqual(rollup["activity_ui_open_count"], 1)
        self.assertEqual(rollup["activity_ui_click_count"], 2)
        self.assertEqual(rollup["activity_threshold_idle_sec"], 15)
        self.assertEqual(rollup["activity_threshold_afk_sec"], 120)


if __name__ == "__main__":
    unittest.main()
