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

class IncrementalApiTests(unittest.TestCase):
    def test_compressed_raw_event_preserves_history_and_rebuilds_facts(self):
        from analytics_facts import decode_live_event_json, migrate_facts
        from import_gameplay_telemetry import ensure_schema
        from telemetry_events import accept_batch

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "events.db")
            with sqlite3.connect(db_path) as conn:
                ensure_schema(conn)

            event = {
                "event_type": "newbie_guide_state",
                "event_real_time_iso": "2026-11-23T00:00:00Z",
                "actor": {"agent_id": "0"},
                "payload": {"surface": "path", "result": "not_visible", "diagnostic": "马铃薯" * 100},
            }
            for event_id, compression in (("old", "0"), ("new", "1")):
                payload = {
                    "user_id": "player",
                    "session_id": "session",
                    "client_version": "unity-wegame-3.1.18.26.11.23.09211d71d",
                    "events": [{**event, "event_id": event_id}],
                }
                with patch.dict(os.environ, {"PAW_COMPRESS_LIVE_EVENT_JSON": compression}):
                    for _ in range(2):
                        self.assertEqual(accept_batch(db_path, payload, {}), {"accepted_event_ids": [event_id]})

            with sqlite3.connect(db_path) as conn:
                raw = dict(conn.execute("SELECT event_id,event_json FROM gameplay_live_events"))
                self.assertIsInstance(raw["old"], str)
                self.assertIsInstance(raw["new"], bytes)
                self.assertLess(len(raw["new"]), len(raw["old"].encode("utf-8")))
                for event_id, stored in raw.items():
                    self.assertEqual(decode_live_event_json(stored),
                                     json.dumps({**event, "event_id": event_id}, ensure_ascii=False))

                sql = """SELECT event_id,event_type,payload_json,metadata_json
                         FROM analytics_event_facts ORDER BY event_id"""
                facts = conn.execute(sql).fetchall()
                self.assertEqual(len(facts), 2)
                before = conn.execute("SELECT MAX(received_at) FROM gameplay_live_events").fetchone()
                conn.execute("DELETE FROM analytics_event_facts")
                conn.execute("DELETE FROM analytics_migrations WHERE name='event_facts_v1'")
                migrate_facts(conn)
                self.assertEqual(conn.execute(sql).fetchall(), facts)
                self.assertEqual(conn.execute("SELECT MAX(received_at) FROM gameplay_live_events").fetchone(), before)

    def test_encrypted_batch_ack_is_durable_and_idempotent(self):
        key=Fernet.generate_key();cipher=Fernet(key)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'PAW_FERNET_KEY':key.decode()}), patch('app.core.config.DB_PATH',str(Path(tmp)/'events.db')):
            from telemetry_events import ensure_event_schema
            with sqlite3.connect(str(Path(tmp)/"events.db")) as migrated:
                ensure_event_schema(migrated)
            client=TestClient(create_app())
            payload={'user_id':'test-user','session_id':'test-session','client_platform':'webgl','events':[{'event_id':'test-event','event_type':'cat_tool_completed','event_real_time_iso':'2026-09-20T00:00:00Z','actor':{'agent_id':'0'}}]}
            body=cipher.encrypt(json.dumps(payload).encode()).decode()
            for _ in range(2):
                response=client.post('/v1/events/batch',content=body,headers={'X-Encrypted':'true','X-Client-Platform':'webgl'})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['accepted_event_ids'],['test-event'])
            with sqlite3.connect(str(Path(tmp)/'events.db')) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*),client_platform FROM gameplay_live_events').fetchone(),(1,'webgl'))
            self.assertEqual(client.post('/v1/events/batch',json=payload).status_code,415)
            client.close()

if __name__=='__main__':unittest.main()
