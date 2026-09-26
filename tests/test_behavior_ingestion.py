"""Exercise the encrypted production intake path, not only reducer fixtures."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from app.main import create_app
from behavior_analytics import METRICS, refresh_behavior
from tools.telemetry_database import migrate


class BehaviorIngestionTests(unittest.TestCase):
    def test_encrypted_participation_and_server_actions_join_and_deduplicate(self):
        key=Fernet.generate_key();cipher=Fernet(key)
        base=datetime(2026,9,1,tzinfo=timezone.utc)
        events=[]
        for i,kind in enumerate(('journey_world_ready','journey_checkpoint','journey_quit')):
            counters=dict.fromkeys(METRICS,0)
            counters.update(version='participation-v1',in_world=True,monotonic_seconds=i*30,active=i*30,fishing=i*30)
            events.append({'event_id':'participation-'+str(i),'event_type':kind,'schema_version':4,'sequence':i+1,
                'event_real_time_iso':(base+timedelta(seconds=i*30)).isoformat(),
                'payload':{'flow_version':'journey-v1','current_node':'world','server_session_id':'server-session','participation':counters}})
        payload={'user_id':'behavior-integration','session_id':'client-run','client_platform':'windows','events':events}
        action={'user_id':'behavior-integration','session_id':'server-session','client_platform':'windows','events':[
            {'event_id':'cast','event_type':'fishing_started','schema_version':3,'sequence':1,
             'event_real_time_iso':(base+timedelta(seconds=1)).isoformat(),'actor':{'is_player':True},
             'payload':{'operation_id':'cast-operation','source':'player'}}]}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'PAW_FERNET_KEY':key.decode()}), patch('app.core.config.DB_PATH',str(Path(tmp)/'events.db')), patch('app.core.lifespan.DB_PATH',str(Path(tmp)/'events.db')):
            migrate(str(Path(tmp)/'events.db'),'wal')
            with TestClient(create_app()) as client:
                for batch in (payload,action,payload,action):
                    response=client.post('/v1/events/batch',content=cipher.encrypt(json.dumps(batch).encode()).decode(),
                        headers={'X-Encrypted':'true','X-Client-Platform':'windows'})
                    self.assertEqual(response.status_code,200,response.text)
                with sqlite3.connect(Path(tmp)/'events.db') as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM behavior_actions').fetchone()[0],1)
                    refresh_behavior(conn)
                    self.assertEqual(conn.execute('SELECT SUM(active) FROM behavior_intervals').fetchone()[0],60)
                    self.assertEqual(conn.execute('SELECT session_id,active,fishing FROM behavior_players').fetchone(),('server-session',60,60))
                    self.assertEqual(conn.execute('SELECT covered FROM behavior_actions').fetchone()[0],1)
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM analytics_event_facts').fetchone()[0],4)
