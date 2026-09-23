import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from import_gameplay_telemetry import ensure_schema, import_sample
from telemetry_platform import client_metadata
from telemetry_events import accept_batch
from playtime_store import record_play_session_event
from generate_analytics_dashboards import build

class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name)/'test.db')
        self.db = sqlite3.connect(self.path)
        self.db.execute('CREATE TABLE user_sessions(user_id TEXT PRIMARY KEY,is_developer INTEGER DEFAULT 0,nickname TEXT,player_name TEXT,is_blacklisted INTEGER DEFAULT 0,tasks_completed INTEGER,tasks_total INTEGER,current_task_title TEXT,current_task_status TEXT,total_play_seconds REAL,last_seen TEXT)')
        self.db.execute('CREATE TABLE conversations(id INTEGER,user_id TEXT,session_id TEXT,timestamp TEXT,message_type TEXT,release_version TEXT,client_version TEXT,llm_request_id TEXT,attempt_id TEXT,user_query TEXT,ai_response TEXT,duration_ms REAL,prompt_tokens INTEGER,completion_tokens INTEGER,is_preset INTEGER DEFAULT 0)')
        ensure_schema(self.db)
        self.db.commit()
    def tearDown(self):
        self.db.close();self.tmp.cleanup()
    def event(self):
        return {'event_id':'event-one','event_real_time_iso':'2026-09-19T10:00:00Z','event_type':'cat_agent_response','actor':{'agent_id':'0','is_player':False},'sequence':1,'payload':{'request_stats':{'llm_request_id':'req-one'},'input_tokens':100,'output_tokens':10}}
    def test_no_server_or_version_inference(self):
        self.assertEqual(client_metadata({'client_version':'unity-dev','runtime_environment':{'platform':'WindowsPlayer'}})['client_platform'],'unknown')
        self.assertEqual(client_metadata({'client_platform':'windows','is_development_build':True})['client_platform'],'windows')
    def test_batch_retry_and_snapshot_deduplicate(self):
        e=self.event();p={'user_id':'u','session_id':'s','player_session_id':'ps','client_platform':'webgl','events':[e]}
        accept_batch(self.path,p,{});accept_batch(self.path,p,{})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analytics_events').fetchone()[0],1)
        sample={'user_id':'u','client_platform':'webgl','gameplay_telemetry':{'session_meta':{'session_id':'s','player_session_id':'ps'},'days':{'1':{'events':[e]}}}}
        import_sample(self.db,source_file='sample',sample=sample,imported_at='2026-09-19T10:01:00Z');self.db.commit()
        rows=self.db.execute('SELECT event_id,client_platform,occurred_at FROM analytics_events').fetchall()
        self.assertEqual(rows,[('event-one','webgl','2026-09-19T10:00:00Z')])
    def test_conflicting_session_and_invalid_timestamp_rejected(self):
        p={'user_id':'u','session_id':'s','events':[self.event()]};accept_batch(self.path,p,{})
        p['session_id']='other'
        with self.assertRaises(ValueError):accept_batch(self.path,p,{})
        p['events'][0]['event_real_time_iso']='bad'
        with self.assertRaises(ValueError):accept_batch(self.path,p,{})
    def test_mixed_platforms_and_global_first_entry(self):
        for i,platform in enumerate(['webgl','windows','editor']):
            payload={'user_id':'u','session_id':str(i),'client_platform':platform,'timestamp':f'2026-09-{17+i}T10:00:00Z'}
            record_play_session_event(self.db,payload=payload,headers={},event_type='login',received_at=payload['timestamp'])
        self.assertEqual(self.db.execute('SELECT first_platform FROM analytics_first_entry').fetchone()[0],'webgl')
        self.assertEqual(tuple(self.db.execute('SELECT COUNT(DISTINCT user_id),COUNT(DISTINCT client_platform) FROM analytics_sessions').fetchone()),(1,3))
    def test_legacy_history_remains_visible_with_default_filters(self):
        event = self.event()
        event.pop('event_real_time_iso')
        sample = {'user_id':'legacy-user','gameplay_telemetry':{'session_meta':{'session_id':'legacy-session'},'days':{'1':{'events':[event]}}}}
        import_sample(self.db,source_file='legacy-sample',sample=sample,imported_at='2026-09-19T10:01:00Z')
        self.db.commit()
        boards = build()
        for board in boards.values():
            platform = next(v for v in board['templating']['list'] if v['name']=='client_platform')
            self.assertIn('unknown', platform['current']['value'])
        overview = boards['gameplay-overview.json']
        sql = next(p for p in overview['panels'] if p['id']==30)['targets'][0]['queryText']
        self.assertNotIn('${__from}', sql)
        self.assertNotIn('${__to}', sql)
        for key,value in {'${client_platform:sqlstring}':"'webgl','windows','unknown'",'${release_version:sqlstring}':"'__all__'",'${test_data}':'auto','${playtest_id}':'all'}.items():
            sql=sql.replace(key,value)
        self.assertEqual(tuple(self.db.execute(sql).fetchone()), (1,1))

    def test_all_dashboard_queries_compile_with_filters(self):
        for platform in ["'webgl','windows'","'editor'","'unknown'","'__all__'"]:
            for board in build().values():
                for panel in board['panels']:
                    for target in panel.get('targets',[]):
                        sql=target.get('queryText')
                        if not sql:continue
                        replacements={'${client_platform:sqlstring}':platform,'${release_version:sqlstring}':"'__all__'",'${test_data}':'auto','${playtest_id}':'all','${user_id:sqlstring}':"''",'${session_id:sqlstring}':"''",'${llm_request_id:sqlstring}':"''",'${page:sqlstring}':"'0'",'${theater_event_id:sqlstring}':"''",'${min_invitations:sqlstring}':"'10'",'${behavior_period}':'all','${sort_field}':'latest_login','${sort_direction}':'desc','${event_type:sqlstring}':"''",'${__from}':'1789257600000','${__to}':'1789862400000'}
                        for key,value in replacements.items():sql=sql.replace(key,value)
                        with self.subTest(board=board['uid'],panel=panel['title'],platform=platform):self.db.execute(sql).fetchall()

if __name__=='__main__':unittest.main()
