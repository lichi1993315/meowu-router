import json
import sqlite3
import unittest

from generate_analytics_dashboards import build


class CampaignDashboardTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''CREATE TABLE analytics_events(event_id TEXT PRIMARY KEY,user_id TEXT,
          session_id TEXT,client_platform TEXT,release_version TEXT,is_development_build INTEGER,
          is_developer INTEGER,occurred_at TEXT,event_type TEXT,payload_json TEXT);
          CREATE TABLE analytics_session_channels(user_id TEXT,session_id TEXT,distribution_channel TEXT);
          CREATE TABLE analytics_session_playtests(user_id TEXT,session_id TEXT,playtest_id TEXT);''')
        self.board=build()['gameplay-campaigns.json']

    def emit(self, eid, user='u', kind='survey_click', action='click', **kw):
        self.db.execute('INSERT OR IGNORE INTO analytics_events VALUES(?,?,?,?,?,?,?,?,?,?)',
            (eid,user,kw.get('session','s'),'windows','steam-1',kw.get('dev',0),0,
             kw.get('at','2026-09-26T20:00:00+08:00'),kind,json.dumps({'action':action,'detail':'main_menu'})))

    def query(self, pid, **overrides):
        q=next(p for p in self.board['panels'] if p['id']==pid)['targets'][0]['queryText']
        values={'client_platform:sqlstring':"'windows'",'release_version:sqlstring':"'__all__'",
            'distribution_channel:sqlstring':"'__all__'",'test_data':'exclude','playtest_id':'all',
            'user_id:sqlstring':"''",'__from':'1788192000000','__to':'1790784000000'}
        values.update(overrides)
        for k,v in values.items(): q=q.replace('${'+k+'}',v)
        self.assertNotIn('${',q)
        return [dict(r) for r in self.db.execute(q)]

    def test_all_queries_empty_and_populated(self):
        for populated in (False,True):
            if populated: self.emit('1')
            for p in self.board['panels']:
                if p.get('targets'): self.query(p['id'])

    def test_repeat_clicks_identity_and_event_id_deduplication(self):
        self.emit('1');self.emit('1');self.emit('2');self.emit('3','anon:device');self.emit('4','unknown')
        row=self.query(10)[0]
        self.assertEqual((row['事件次数'],row['已登录玩家数'],row['匿名身份数'],row['身份缺失事件数']),(4,1,1,1))
        self.assertEqual(self.query(1)[0]['玩家数'],1)

    def test_client_receipt_does_not_count_as_server_grant(self):
        self.emit('1',kind='wishlist_cta',action='claimed')
        self.assertEqual(self.query(4)[0]['玩家数'],0)
        self.emit('2',kind='wishlist_reward_claimed',action='')
        self.assertEqual(self.query(4)[0]['玩家数'],1)
        self.assertEqual(self.query(3)[0]['玩家数'],0)

    def test_time_test_user_and_batch_filters(self):
        self.emit('1');self.emit('2','dev',dev=1);self.emit('3','old',at='2025-01-01T00:00:00Z')
        self.assertEqual(self.query(1)[0]['玩家数'],1)
        self.assertEqual(self.query(1,test_data='include')[0]['玩家数'],2)
        self.assertEqual(self.query(1,playtest_id='中秋playtest')[0]['玩家数'],0)
        self.assertEqual(self.query(1,**{'user_id:sqlstring':"'absent'"})[0]['玩家数'],0)
        batch=next(v for v in self.board['templating']['list'] if v['name']=='playtest_id')
        self.assertEqual(batch['current']['value'],'all')

    def test_local_day_and_all_dashboard_navigation(self):
        self.emit('1',at='2026-09-26T20:00:00Z')
        self.assertEqual(self.query(12)[0]['日期'],'2026-09-27')
        for board in build().values():
            self.assertTrue(any(x['url']=='/d/gameplay-campaigns' for x in board['links']))
