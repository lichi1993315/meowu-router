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
    def test_encrypted_batch_ack_is_durable_and_idempotent(self):
        key=Fernet.generate_key();cipher=Fernet(key)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'PAW_FERNET_KEY':key.decode()}), patch('app.core.config.DB_PATH',str(Path(tmp)/'events.db')):
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
