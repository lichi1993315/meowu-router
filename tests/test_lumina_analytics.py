import json
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from lumina_analytics import ensure_lumina_schema
import lumina_dashboards


def ev(session, seq, kind, at, *, player='p1', eff=0.0, where=None, data=None, **extra):
    return {'event_id': f'{session}:{seq}', 'session_id': session, 'player_id': player, 'seq': seq, 'type': kind,
            't': at.isoformat().replace('+00:00', 'Z'), 'eff_s': eff, 'fg_s': eff, 'afk_s': 0, 'bg_s': 0, 'v': 1,
            'build': 'abc123', 'platform': 'desktop', 'tb': 'pt1', 'src': None, 'internal': False, 'session_n': extra.pop('n', 1),
            'archive_id': 'arc', 'where': where or {}, 'data': data or {}, **extra}


def substitute(sql, tb='__all__', audience='external', scope='first'):
    now = datetime.now(timezone.utc)
    sql = sql.replace('${__from}', str(int((now - timedelta(days=30)).timestamp() * 1000)))
    sql = sql.replace('${__to}', str(int((now + timedelta(days=1)).timestamp() * 1000)))
    for name, value in (('tb', tb), ('audience', audience), ('scope', scope)):
        sql = sql.replace('${%s:sqlstring}' % name, "'%s'" % value)
    assert not re.search(r'\$\{', sql), sql
    return sql


class LuminaIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'lumina.db')
        with sqlite3.connect(self.db) as conn:
            ensure_lumina_schema(conn)
        self.env = patch.dict(os.environ, {'LUMINA_INGEST_TOKENS': 'tok-a,tok-b', 'LUMINA_ALLOWED_ORIGINS': 'https://game.example',
                                           'LUMINA_RATE_PER_MINUTE': '1000'})
        self.env.start()
        self.path = patch('app.core.config.DB_PATH', self.db)
        self.path.start()
        self.client = TestClient(create_app())

    def tearDown(self):
        self.client.close()
        self.path.stop()
        self.env.stop()
        self.tmp.cleanup()

    def post(self, events, token='tok-a', origin='https://game.example'):
        return self.client.post('/v1/lumina/events', content=json.dumps({'token': token, 'events': events}),
                                headers={'Content-Type': 'text/plain', 'Origin': origin})

    def q(self, sql, *args):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(sql, args).fetchall()

    def test_batch_is_idempotent_and_rolls_up_once(self):
        t = datetime.now(timezone.utc) - timedelta(hours=1)
        events = [ev('s1', 1, 'boot', t, where={'stage': 'boot'}),
                  ev('s1', 2, 'load_stage', t + timedelta(seconds=2), where={'stage': 'Loading characters…'}),
                  ev('s1', 3, 'ready', t + timedelta(seconds=9), data={'load_ms': 7000, 'page_ms': 9000}),
                  ev('s1', 4, 'step_start', t + timedelta(seconds=10), eff=1, where={'task': 'first_harvest', 'step': 'gather_mushroom'},
                     data={'task': 'first_harvest', 'step': 'gather_mushroom', 'task_i': 0, 'step_i': 0, 'label': '首次收获 · 采集野生蘑菇'}),
                  ev('s1', 5, 'heartbeat', t + timedelta(seconds=25), eff=16, where={'task': 'first_harvest', 'step': 'gather_mushroom'})]
        for _ in range(2):
            r = self.post(events)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()['accepted_event_ids'], [e['event_id'] for e in events])
            self.assertEqual(r.headers.get('access-control-allow-origin'), 'https://game.example')
        self.assertEqual(self.q('SELECT COUNT(*) FROM lumina_events'), [(5,)])
        row = self.q('SELECT events, load_ms, page_ms, max_eff_s, last_step, stage, act_type FROM lumina_sessions')[0]
        self.assertEqual(row, (5, 7000, 9000, 16.0, 'gather_mushroom', 'Loading characters…', 'step_start'))

    def test_out_of_order_keeps_latest_place(self):
        t = datetime.now(timezone.utc)
        self.post([ev('s2', 3, 'heartbeat', t, eff=40, where={'task': 'a', 'step': 'late'})])
        self.post([ev('s2', 2, 'step_done', t, eff=30, where={'task': 'a', 'step': 'early'}, data={'step': 'x', 'task': 'a', 'eff': 12})])
        self.assertEqual(self.q('SELECT last_step, max_eff_s, act_type FROM lumina_sessions'), [('late', 40.0, 'step_done')])
        self.assertEqual(self.q('SELECT step, dur_eff FROM lumina_steps'), [('x', 12.0)])

    def test_rejections(self):
        t = datetime.now(timezone.utc)
        self.assertEqual(self.post([ev('s3', 1, 'boot', t)], token='nope').status_code, 401)
        bad = ev('s3', 1, 'boot', t)
        bad['event_id'] = 'other:1'
        self.assertEqual(self.post([bad]).status_code, 400)
        bad = ev('s3', 1, 'Bad Type', t)
        self.assertEqual(self.post([bad]).status_code, 400)
        self.assertEqual(self.client.post('/v1/lumina/events', content='not json').status_code, 400)
        r = self.post([ev('s3', 1, 'boot', t)], origin='https://evil.example')
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.headers.get('access-control-allow-origin'))
        self.assertEqual(self.q('SELECT COUNT(*) FROM lumina_events'), [(1,)])
        with patch.dict(os.environ, {'LUMINA_INGEST_TOKENS': ''}):
            self.assertEqual(self.post([ev('s3', 2, 'boot', t)]).status_code, 503)

    def test_dashboard_queries_run_and_count(self):
        old = datetime.now(timezone.utc) - timedelta(days=2)
        # five ended first sessions: one left while loading, others after 20 s, 90 s, 4 min and 12 min
        batches = [[ev('L', 1, 'boot', old, player='pL', where={'stage': 'boot'}),
                    ev('L', 2, 'load_stage', old + timedelta(seconds=5), player='pL', where={'stage': 'Warming up shaders…'})]]
        for i, secs in enumerate([20, 90, 240, 720]):
            sid, pid = f'S{i}', f'p{i}'
            step = {'task': 'first_harvest', 'step': 'gather_mushroom'}
            b = [ev(sid, 1, 'boot', old, player=pid), ev(sid, 2, 'ready', old + timedelta(seconds=8), player=pid, data={'load_ms': 6000, 'page_ms': 8000}),
                 ev(sid, 3, 'step_start', old + timedelta(seconds=9), player=pid, eff=0, where=step,
                    data={**step, 'task_i': 0, 'step_i': 0, 'label': '首次收获 · 采集野生蘑菇'})]
            if secs > 60:
                b.append(ev(sid, 4, 'step_done', old + timedelta(seconds=60), player=pid, eff=50, where=step, data={**step, 'task_i': 0, 'step_i': 0, 'eff': 50}))
                step = {'task': 'first_harvest', 'step': 'enter_restaurant'}
                b.append(ev(sid, 5, 'step_start', old + timedelta(seconds=61), player=pid, eff=50, where=step, data={**step, 'task_i': 0, 'step_i': 1}))
            b.append(ev(sid, 9, 'heartbeat', old + timedelta(seconds=secs + 10), player=pid, eff=secs, where=step))
            batches.append(b)
        batches.append([ev('S0b', 1, 'boot', old + timedelta(days=1), player='p0', n=2)])  # p0 came back
        batches.append([ev('S1', 6, 'error', old + timedelta(seconds=30), player='p1', data={'kind': 'error', 'message': 'boom', 'at': 'main.js:1'})])
        for b in batches:
            self.assertEqual(self.post(b).status_code, 200)
        with sqlite3.connect(self.db) as conn:
            def run(sql, **kw):
                return conn.execute(substitute(sql, **kw)).fetchall()
            for p in lumina_dashboards.panels():
                for t in p.get('targets', []):
                    run(t['queryText'])
                    run(t['queryText'], tb='pt1', audience='all', scope='all')
            stats = {title: run(sql)[0][0] for title, sql, _ in lumina_dashboards.STATS}
            hazard = run(lumina_dashboards.HAZARD)
            funnel = run(lumina_dashboards.FUNNEL)
            survival = dict(run(lumina_dashboards.SURVIVAL))
        self.assertEqual(stats['会话'], 5)
        self.assertEqual(stats['加载成功率'], 80.0)
        self.assertEqual(stats['回访率（首会话满 24h）'], 20.0)
        self.assertEqual(hazard[0][:4], ('加载中', 5, 1, 20.0))
        self.assertEqual(hazard[0][4], '加载：Warming up shaders…')
        self.assertEqual(hazard[1][:3], ('0–30s', 4, 1))
        self.assertEqual(funnel[0][1:6], ('首次收获 · 采集野生蘑菇', 4, 3, 75.0, 50.0))
        self.assertEqual(funnel[0][8], 1)  # S0 left on the first step
        self.assertEqual(survival['0.0'], 100.0)
        self.assertEqual(survival['1.0'], 75.0)


class DashboardFileTests(unittest.TestCase):
    def test_checked_in_json_is_generated(self):
        self.assertEqual(json.loads(lumina_dashboards.OUT.read_text(encoding='utf-8')), json.loads(json.dumps(lumina_dashboards.dashboard())))


if __name__ == '__main__':
    unittest.main()
