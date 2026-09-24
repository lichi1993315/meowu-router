"""Regression coverage for session cohort boundaries and multi-model cost totals."""
import json
import sqlite3
import unittest
from pathlib import Path

from playtime_store import ensure_playtime_schema, record_play_session_event
from import_gameplay_telemetry import normalize_ai_token_usage, extract_event_ai_usage, aggregate_ai_usages, ensure_schema


class PlaytestCostTests(unittest.TestCase):
    def test_only_new_logins_are_marked_and_retries_do_not_move_boundary(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        ensure_playtime_schema(db)
        db.execute("INSERT INTO analytics_playtests VALUES ('中秋playtest','2026-09-23T10:00:00Z')")
        def record(session, kind, sent, received='2026-09-23T11:00:00Z'):
            record_play_session_event(db,payload={'user_id':'a','session_id':session,'timestamp':sent,'client_platform':'windows'},headers={},event_type=kind,received_at=received)
        record('old','login','2026-09-23T09:00:00Z')
        record('old','heartbeat','2026-09-23T10:05:00Z')
        record('old','login','2026-09-23T10:10:00Z')
        record('new','login','2026-09-23T10:01:00Z')
        record('new','login','2026-09-23T10:01:00Z')
        record('missing','login','')
        record('heartbeat-only','heartbeat','2026-09-23T10:30:00Z')
        self.assertEqual([tuple(x) for x in db.execute('SELECT * FROM analytics_session_playtests')],[('a','new','中秋playtest')])
        ensure_playtime_schema(db)
        self.assertEqual(db.execute('SELECT started_at FROM analytics_playtests').fetchone()[0],'2026-09-23T10:00:00Z')
        db.close()

    def test_known_platform_and_september_24_boundary_repair(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        ensure_playtime_schema(db)
        db.execute("INSERT INTO analytics_playtests VALUES ('中秋playtest','2026-09-23T14:50:19.335Z')")
        cases = [('before', 'windows', '2026-09-23T15:59:59Z'),
                 ('boundary', 'windows', '2026-09-23T16:00:00Z'),
                 ('web', 'webgl', '2026-09-24T00:00:00Z'),
                 ('editor', 'editor', '2026-09-24T00:00:00Z'),
                 ('other', 'other', '2026-09-24T00:00:00Z'),
                 ('unknown', 'unknown', '2026-09-24T00:00:00Z'),
                 ('legacy', 'unattributed', '2026-09-24T00:00:00Z'),
                 ('empty', '', '2026-09-24T00:00:00Z'),
                 ('missing', None, '2026-09-24T00:00:00Z')]
        for session, platform, sent in cases:
            record_play_session_event(db, payload={'user_id':'a', 'session_id':session,
                'timestamp':sent, 'client_platform':platform}, headers={}, event_type='login',
                received_at='2026-09-24T01:00:00Z')
        self.assertEqual({r[0] for r in db.execute('SELECT session_id FROM analytics_session_playtests')},
                         {'before', 'boundary', 'web', 'editor', 'other'})
        # Simulate tags written by the old rule, then repair them without deleting events.
        for session, _, _ in cases:
            db.execute("INSERT OR IGNORE INTO analytics_session_playtests VALUES ('a',?,'中秋playtest')", (session,))
        migration = (Path(__file__).resolve().parents[1] / 'migrations/20260924_playtest_boundary.sql').read_text()
        for _ in range(2):
            db.executescript(migration)
            self.assertEqual({r[0] for r in db.execute('SELECT session_id FROM analytics_session_playtests')},
                             {'boundary', 'web', 'editor', 'other'})
            self.assertEqual(db.execute('SELECT COUNT(*) FROM play_session_events').fetchone()[0], len(cases))
        self.assertEqual(db.execute('SELECT started_at FROM analytics_playtests').fetchone()[0],
                         '2026-09-23T16:00:00Z')
        record_play_session_event(db, payload={'user_id':'a', 'session_id':'late-old-login',
            'timestamp':'2026-09-23T15:59:59Z', 'client_platform':'windows'}, headers={},
            event_type='login', received_at='2026-09-24T02:00:00Z')
        self.assertIsNone(db.execute("SELECT 1 FROM analytics_session_playtests WHERE session_id='late-old-login'").fetchone())
        db.close()

    def test_multi_model_total_is_authoritative_without_flat_rates(self):
        raw={'session_input_tokens':1000,'session_output_tokens':100,'session_estimated_usd':0.032}
        self.assertEqual(normalize_ai_token_usage(raw,source='session')['estimated_cost_usd'],0.032)
        raw['session_estimated_usd']=0
        self.assertEqual(normalize_ai_token_usage(raw,source='session')['estimated_cost_usd'],0)
        raw['session_estimated_usd']=None
        self.assertIsNone(normalize_ai_token_usage(raw,source='session')['estimated_cost_usd'])

    def test_per_call_reported_cost_and_unknown_survive_aggregation(self):
        payload={'response_stats':{'prompt_tokens':1000,'completion_tokens':10},'estimated_usd':0.021}
        known=extract_event_ai_usage(payload)
        self.assertEqual(known['estimated_cost_usd'],0.021)
        unknown=extract_event_ai_usage({'prompt_tokens':1000})
        self.assertIsNone(unknown['estimated_cost_usd'])
        self.assertIsNone(aggregate_ai_usages([known,unknown])['estimated_cost_usd'])
        self.assertEqual(aggregate_ai_usages([known,known])['estimated_cost_usd'],0.042)

    def test_existing_session_cost_is_repaired_idempotently(self):
        db=sqlite3.connect(':memory:')
        db.execute('CREATE TABLE user_sessions(user_id TEXT,is_developer INTEGER,nickname TEXT,player_name TEXT)')
        ensure_schema(db)
        for sid,cost in [('known',0.052),('unknown',None)]:
            meta=json.dumps({'ai_token_usage':{'session_total_tokens':1000,'session_estimated_usd':cost}})
            db.execute('INSERT INTO gameplay_sessions(source_file,user_id,session_id,imported_at,session_meta_json,ai_estimated_cost_usd) VALUES (?,?,?,?,?,0)',(sid,'u',sid,'2026-09-23',meta))
        for _ in range(2):
            ensure_schema(db)
            self.assertEqual(db.execute('SELECT ai_estimated_cost_usd FROM gameplay_sessions ORDER BY session_id').fetchall(),[(0.052,),(None,)])
        db.close()
