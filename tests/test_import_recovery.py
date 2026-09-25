"""A failed database commit must never acknowledge its source file."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import import_gameplay_telemetry as importer
import metrics_exporter as metrics
from tools.telemetry_database import migrate
from sqlite_runtime import connection


class ImportRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "conversations.db"
        migrate(self.db, "wal")

    def tearDown(self):
        self.tmp.cleanup()

    def test_metrics_failure_does_not_mark_source_processed(self):
        output = self.root / "output"
        (output / "u").mkdir(parents=True)
        path = output / "u" / "request.jsonl"
        path.write_text(json.dumps({"type": "request", "user_id": "u", "body": {}, "headers": {}})
                        + "\n" + json.dumps({"type": "response", "body": {}}))
        state = metrics.MetricsState(self.root)
        with patch.object(metrics, "OUTPUT_DIR", output), patch.object(state, "recalculate_user_sessions"), \
                patch.object(state, "update_user_gauges"), patch.object(state, "save_conversation", side_effect=sqlite3.OperationalError("database is locked")):
            metrics.scan_output_directory(state)
        self.assertNotIn(str(path), state.processed_files)
        self.assertIn(str(path), state.file_retry_after)
        state.file_retry_after.clear()
        with patch.object(metrics, "OUTPUT_DIR", output):
            metrics.scan_output_directory(state)
        self.assertIn(str(path), state.processed_files)
        with connection(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM conversations").fetchone()[0], 1)

    def test_importer_failure_rolls_back_and_does_not_advance_mtime(self):
        sources = self.root / "sources"
        sources.mkdir()
        source = sources / "failed.json"
        source.write_text('{}')
        state_path = self.root / "state.json"
        def interrupted(conn, path, **kwargs):
            self.assertFalse(conn.in_transaction)
            def publish():
                conn.execute("INSERT INTO journey_dirty(user_id) VALUES ('uncommitted')")
                raise RuntimeError("simulated failure before commit")
            return publish
        with patch.multiple(importer, DB_PATH=self.db, TELEMETRY_DIR=sources,
                            OUTPUT_DIR=self.root / "empty", STATE_PATH=state_path), \
                patch.object(importer, "prepare_import_file", interrupted):
            self.assertEqual(importer.run_import_once(), 0)
        self.assertNotIn(str(source.resolve()), importer.ImportState.load(state_path).files)
        with connection(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM journey_dirty WHERE user_id='uncommitted'").fetchone()[0], 0)

    def test_runtime_import_builds_rows_before_acquiring_writer(self):
        sources=self.root/'sources';sources.mkdir()
        path=sources/'sample.json'
        path.write_text(json.dumps({'user_id':'u','gameplay_telemetry':{
            'session_meta':{'session_id':'s'},'days':{'0':{'events':[
                {'event_type':'test','sequence':i,'payload':{'nested':{'value':i}}} for i in range(100)]}}}}))
        original=importer.prepare_sample
        prepared=[]
        def observe(conn, **kwargs):
            self.assertFalse(conn.in_transaction)
            publish=original(conn,**kwargs)
            prepared.append(True)
            def write():
                self.assertTrue(conn.in_transaction)
                return publish()
            return write
        with patch.multiple(importer,DB_PATH=self.db,TELEMETRY_DIR=sources,
                OUTPUT_DIR=self.root/'empty',STATE_PATH=self.root/'state.json'), \
                patch.object(importer,'prepare_sample',observe):
            self.assertEqual(importer.run_import_once(),1)
        self.assertEqual(prepared,[True])
        with connection(self.db) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM gameplay_events').fetchone()[0],100)

    def test_journey_new_event_during_computation_remains_pending(self):
        import journey_analytics as journey
        from analytics_facts import upsert_fact
        def emit(conn, eid):
            upsert_fact(conn, {"event_id": eid, "event_type": "journey_started", "payload": {},
                "event_real_time_iso": "2026-09-25T11:28:00Z"}, "u", "s", {}, "2026-09-25T11:28:00Z")
            conn.commit()
        with connection(self.db) as conn:
            emit(conn, "first")
            original = journey.project
            def concurrent(points):
                self.assertFalse(conn.in_transaction)
                with connection(self.db) as writer:
                    emit(writer, "second")
                return original(points)
            with patch.object(journey, "project", concurrent):
                journey.refresh_journeys(conn)
            self.assertEqual(conn.execute("SELECT pending,revision FROM journey_dirty WHERE user_id='u'").fetchone(), (1, 2))
            self.assertEqual(conn.execute("SELECT count(*) FROM journey_players").fetchone()[0], 0)
            journey.refresh_journeys(conn)
            self.assertEqual(conn.execute("SELECT pending FROM journey_dirty WHERE user_id='u'").fetchone()[0], 0)

    def test_unchanged_projection_does_not_rewrite_historical_intervals(self):
        import journey_analytics as journey
        from analytics_facts import upsert_fact
        with connection(self.db) as conn:
            for i in range(3):
                upsert_fact(conn, {"event_id": str(i), "event_type": "journey_checkpoint", "payload": {},
                    "event_real_time_iso": f"2026-09-25T11:28:0{i}Z"}, "u", "s", {}, "2026-09-25T11:28:00Z")
            conn.commit()
            journey.refresh_journeys(conn)
            before=conn.execute("SELECT * FROM journey_intervals").fetchall()
            conn.execute("UPDATE journey_dirty SET pending=1,revision=revision+1")
            conn.commit()
            statements=[]
            conn.set_trace_callback(statements.append)
            journey.refresh_journeys(conn)
            self.assertEqual(conn.execute("SELECT * FROM journey_intervals").fetchall(),before)
            self.assertFalse(any("journey_intervals" in sql and sql.startswith(("INSERT", "DELETE", "UPDATE")) for sql in statements))

    def test_projection_timeout_keeps_previous_snapshot_and_defers(self):
        import journey_analytics as journey
        from analytics_facts import upsert_fact
        from sqlite_runtime import transaction
        with connection(self.db) as conn:
            upsert_fact(conn, {"event_id":"one", "event_type":"journey_checkpoint", "payload":{},
                "event_real_time_iso":"2026-09-25T11:28:00Z"}, "u", "s", {}, "2026-09-25T11:28:00Z")
            conn.commit()
            journey.refresh_journeys(conn)
            before=conn.execute("SELECT * FROM journey_players").fetchall()
            conn.execute("UPDATE journey_dirty SET pending=1,revision=revision+1")
            conn.commit()
            def limited(conn, operation, **kwargs):
                return transaction(conn, operation, budget=0 if operation=='journey.publish' else 1)
            with patch.object(journey,'transaction',limited):
                journey.refresh_journeys(conn)
            self.assertEqual(conn.execute("SELECT * FROM journey_players").fetchall(),before)
            pending,retry=conn.execute("SELECT pending,retry_after FROM journey_dirty").fetchone()
            self.assertEqual(pending,1)
            self.assertGreater(retry,0)


if __name__ == "__main__":
    unittest.main()
