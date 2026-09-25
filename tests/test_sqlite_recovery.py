"""Real SQLite contention and replay regression tests; no external services."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlite_runtime import connection, transaction, require_schema, MigrationRequired, TransactionBudgetExceeded, is_busy
from telemetry_events import ensure_event_schema, accept_batch
from playtime_store import ensure_playtime_schema, record_play_session_event_to_db


class SqliteRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "telemetry.db"
        with connection(self.path) as conn:
            ensure_event_schema(conn)
            ensure_playtime_schema(conn)
            conn.commit()
            conn.execute("PRAGMA journal_mode=WAL")
        self.payload = {"user_id": "u", "session_id": "s", "events": [{
            "event_id": "e1", "event_real_time_iso": "2026-09-25T11:28:00Z", "event_type": "test"}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_migration_does_not_create_tables(self):
        with connection(Path(self.tmp.name) / "empty.db") as conn:
            with self.assertRaises(MigrationRequired):
                require_schema(conn, "events")
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)

    def test_long_reader_does_not_block_wal_writer(self):
        with connection(self.path) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM gameplay_live_events").fetchall()
            start = time.monotonic()
            accept_batch(self.path, self.payload, {})
            self.assertLess(time.monotonic() - start, 1)
            # Reader owns an older snapshot; a new read sees the committed event.
            self.assertEqual(reader.execute("SELECT count(*) FROM gameplay_live_events").fetchone()[0], 0)
        with connection(self.path) as reader:
            self.assertEqual(reader.execute("SELECT count(*) FROM gameplay_live_events").fetchone()[0], 1)

    def test_writer_timeout_then_retry_is_idempotent(self):
        with connection(self.path) as holder:
            holder.execute("BEGIN IMMEDIATE")
            start = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError) as raised:
                accept_batch(self.path, self.payload, {})
            self.assertTrue(is_busy(raised.exception))
            self.assertGreaterEqual(time.monotonic() - start, 9)
            self.assertLess(time.monotonic() - start, 12)
            holder.rollback()
        accept_batch(self.path, self.payload, {})
        accept_batch(self.path, self.payload, {})
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM gameplay_live_events").fetchone()[0], 1)

    def test_budget_rolls_back_and_connection_can_be_reused(self):
        with connection(self.path) as conn:
            with self.assertRaises(TransactionBudgetExceeded):
                with transaction(conn, "test.budget", budget=.001):
                    conn.execute("INSERT INTO journey_dirty(user_id) VALUES ('u')")
                    time.sleep(.005)
            self.assertEqual(conn.execute("SELECT count(*) FROM journey_dirty").fetchone()[0], 0)
            with transaction(conn, "test.recovery", budget=1):
                conn.execute("INSERT INTO journey_dirty(user_id) VALUES ('u')")

    def heartbeat(self, sequence, sent, received, state="foreground", kind="heartbeat"):
        return record_play_session_event_to_db(db_path=self.path, payload={
            "user_id": "u", "session_id": "s", "timestamp": sent, "sequence": sequence,
            "game_duration_sec": sequence * 30, "app_state": state,
            "activity_since_last_heartbeat": {"input_event_count": 1}},
            headers={"x-outbox-id": "outbox-" + str(sequence)}, event_type=kind, received_at=received)

    def test_old_heartbeat_cannot_rewind_state_or_reopen_logoff(self):
        self.heartbeat(2, "2026-09-25T11:29:00Z", "2026-09-25T11:29:01Z", "background")
        rollup = self.heartbeat(1, "2026-09-25T11:28:30Z", "2026-09-25T11:30:01Z")
        self.assertEqual(rollup["app_state"], "background")
        self.assertEqual(rollup["final_duration_sec"], 60)
        self.heartbeat(3, "2026-09-25T11:30:00Z", "2026-09-25T11:30:02Z", "quitting", "logoff")
        rollup = self.heartbeat(1, "2026-09-25T11:28:30Z", "2026-09-25T11:31:00Z")
        self.assertEqual(rollup["status"], "closed")
        self.assertEqual(rollup["app_state"], "quitting")
        self.assertEqual(rollup["heartbeat_count"], 2)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT received_at FROM play_session_events WHERE outbox_id='outbox-1'").fetchone()[0], "2026-09-25T11:30:01Z")

    def test_no_ddl_in_batch_hot_path(self):
        statements = []
        original = sqlite3.connect
        def traced(*args, **kwargs):
            conn = original(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn
        with patch("sqlite_runtime.sqlite3.connect", traced):
            accept_batch(self.path, self.payload, {})
        self.assertFalse(any(s.lstrip().upper().startswith(("CREATE ", "ALTER ", "DROP ")) for s in statements))

    def test_encrypted_busy_response_is_retryable_and_does_not_ack(self):
        import os
        from cryptography.fernet import Fernet
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes.system import router
        key = Fernet.generate_key()
        app = FastAPI()
        app.include_router(router)
        with patch.dict(os.environ, {"PAW_FERNET_KEY": key.decode()}), \
                patch("app.core.config.DB_PATH", str(self.path)), connection(self.path) as holder:
            holder.execute("BEGIN IMMEDIATE")
            with TestClient(app) as client:
                response = client.post("/v1/events/batch", headers={"X-Encrypted": "true"},
                    content=Fernet(key).encrypt(json.dumps(self.payload).encode()))
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["Retry-After"], "15")
            self.assertEqual(response.json()["code"], "telemetry_store_busy")
            self.assertNotIn("accepted_event_ids", response.json())


if __name__ == "__main__":
    unittest.main()
