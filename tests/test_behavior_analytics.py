import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from analytics_facts import ensure_facts, upsert_fact
from behavior_analytics import METRICS, STATES, CATEGORIES, profile, refresh_behavior, ensure_behavior_schema, backfill_existing_behavior
from generate_analytics_dashboards import build

BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)


class BehaviorTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:'); ensure_facts(self.db)
        self.db.executescript('''CREATE TABLE user_sessions(user_id TEXT,is_developer INTEGER);
            CREATE TABLE analytics_session_channels(user_id TEXT,session_id TEXT,distribution_channel TEXT);
            CREATE TABLE analytics_session_playtests(user_id TEXT,session_id TEXT,playtest_id TEXT);''')
        self.seq = {}; self.ids = 0

    def emit(self, t, kind='journey_checkpoint', user='u', run='r', totals=None, **extra):
        self.ids += 1; self.seq[run] = self.seq.get(run, 0)+1
        p = {'participation': dict.fromkeys(METRICS, 0), 'server_session_id': 'server-'+run, 'current_node':'world'}
        p['participation'].update(version='participation-v1', monotonic_seconds=t, in_world=True)
        p['participation'].update(totals or {}); p.update(extra)
        event = {'event_id': str(self.ids), 'sequence': self.seq[run], 'event_type':kind, 'schema_version':4,
                 'event_real_time_iso': (BASE+timedelta(seconds=t)).isoformat(), 'actor':{'is_player': True}, 'payload': p}
        upsert_fact(self.db, event, user, run, {'client_platform':'windows'}, event['event_real_time_iso'], release='test')
        self.db.commit()
        return event

    def flow(self, user='u', category='ai', run='r', duration=600, offset=0):
        self.emit(offset, 'journey_node_ended', user=user, run=run, node_id='player_create', status='completed')
        self.emit(offset, 'journey_world_ready', user=user, run=run)
        for t in range(30, duration+1, 30):
            self.emit(offset+t, 'journey_quit' if t == duration else 'journey_checkpoint', user=user, run=run,
                      totals={'active':t, category:t})
        refresh_behavior(self.db)

    def player(self, user='u'):
        cursor=self.db.execute('SELECT * FROM behavior_players WHERE user_id=?',(user,))
        row=cursor.fetchone()
        return dict(zip([c[0] for c in cursor.description],row)) if row else None

    def query(self, board, pid, **values):
        sql=next(p for p in build()[board+'.json']['panels'] if p['id']==pid)['targets'][0]['queryText']
        variables={'client_platform:sqlstring':"'__all__'", 'distribution_channel:sqlstring':"'__all__'",
            'release_version:sqlstring':"'__all__'",'test_data':'include','playtest_id':'all',
            'user_id:sqlstring':"''",'session_id:sqlstring':"''",'page:sqlstring':"'0'",'behavior_cohort:sqlstring':"'all'",
            '__from':'0','__to':'2000000000000'}
        variables.update(values)
        for key,value in variables.items(): sql=sql.replace('${'+key+'}',value)
        sql=sql.replace("julianday('now')", "julianday('"+values.get('cutoff','2026-09-10T00:00:00Z')+"')")
        self.assertNotIn('${',sql)
        cursor=self.db.execute(sql)
        return [dict(zip([c[0] for c in cursor.description],r)) for r in cursor]

    def test_empty_and_populated_dashboard_sql(self):
        for populated in (False, True):
            if populated: self.flow()
            for name,board in build().items():
                if name.startswith('gameplay-behavior-'):
                    for panel in board['panels']:
                        if panel.get('targets'): self.query(name[:-5],panel['id'])

    def test_early_window_frozen_and_dynamic_maturity(self):
        self.flow()
        p=self.player(); self.assertEqual(p['preference'],'ai');self.assertEqual(p['active'],600)
        self.flow(run='later',offset=90000,category='farming',duration=1200)
        self.assertEqual(self.player()['preference'],'ai');self.assertEqual(self.player()['active'],600)
        rows=self.query('gameplay-behavior-cohorts',1,cutoff='2026-09-01T12:00:00Z')
        self.assertEqual(rows,[])
        row=self.query('gameplay-behavior-cohorts',1)[0]
        self.assertEqual(row['后续分钟P50'],20);self.assertEqual(row['D7主动回访率'],0)

    def test_no_return_is_zero_not_dropped(self):
        self.flow()
        row=self.query('gameplay-behavior-cohorts',1)[0]
        self.assertEqual(row['后续分钟P50'],0);self.assertEqual(row['D7有效分母'],1)
        self.assertAlmostEqual(row['置信上界'],79.35,places=2)

    def test_immature_return_is_null(self):
        self.flow()
        row=self.query('gameplay-behavior-cohorts',1,cutoff='2026-09-03T00:00:00Z')[0]
        self.assertIsNone(row['D7主动回访率']);self.assertIsNone(row['后续分钟P50'])

    def test_d7_return_outside_selected_cohort_time_range(self):
        self.flow();self.flow(run='later',offset=7*86400+60,duration=60)
        end=str(int((BASE+timedelta(days=1)).timestamp()*1000))
        row=self.query('gameplay-behavior-cohorts',1,**{'__to':end})[0]
        self.assertEqual(row['D7主动回访率'],100)

    def test_duplicate_snapshot_is_idempotent(self):
        self.flow()
        before=self.db.execute('SELECT * FROM behavior_players').fetchall()
        e=self.emit(600,'journey_quit',totals={'active':600,'ai':600})
        upsert_fact(self.db,e,'u','r',{'client_platform':'windows'},e['event_real_time_iso'],release='test')
        self.db.commit()
        refresh_behavior(self.db)
        self.assertEqual(before,self.db.execute('SELECT * FROM behavior_players').fetchall())

    def test_missing_protocol_is_not_idle(self):
        self.emit(0,'journey_world_ready',participation={})
        self.emit(60,'journey_quit',participation={});refresh_behavior(self.db)
        p=self.player();self.assertEqual(p['quality'],'missing_early');self.assertEqual(p['preference'],'insufficient')

    def test_invalid_counter_and_gap_withhold_profile(self):
        for invalid in ({'active':30,'ai':40}, {'active':-1}, {'active':float('inf')}):
            with self.subTest(invalid=invalid):
                # JSON infinity is intentionally not used: SQLite rejects non-JSON before projection.
                if any(v == float('inf') for v in invalid.values()): continue
                self.db.execute('DELETE FROM analytics_event_facts');self.seq={}
                self.emit(0,'journey_world_ready');self.emit(30,'journey_quit',totals=invalid)
                refresh_behavior(self.db)
                self.assertEqual(self.player()['quality'],'incomplete')

    def test_future_gap_is_unknown_not_zero(self):
        self.flow();self.emit(90000,'journey_world_ready',run='later')
        self.emit(91000,'journey_quit',run='later',totals={'active':1000,'ai':1000});refresh_behavior(self.db)
        row=self.query('gameplay-behavior-cohorts',1)[0]
        self.assertIsNone(row['后续分钟P50']);self.assertEqual(row['后续时长样本'],0)

    def test_concurrent_runs_withhold_both(self):
        self.flow();self.flow(run='r2')
        self.assertEqual(self.player()['quality'],'incomplete')
        self.assertEqual(self.player()['preference'],'insufficient')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM behavior_intervals WHERE quality='observed'").fetchone()[0],0)

    def test_automatic_cat_and_exposure_not_opening_actions(self):
        self.emit(0,'journey_world_ready')
        self.emit(1,'theater_view');e=self.emit(2,'farming_harvest')
        e['actor']['is_player']=False
        upsert_fact(self.db,e,'u','r',{'client_platform':'windows'},e['event_real_time_iso'])
        self.emit(3,'player_cat_interaction',pet_uid=0,cat_name='猫猫')
        refresh_behavior(self.db)
        rows=self.query('gameplay-behavior-opening',1)
        self.assertEqual({r['玩法'] for r in rows},{'猫亲密互动'})
        self.assertEqual(self.query('gameplay-behavior-timeline',1),[])
        self.assertEqual(len(self.query('gameplay-behavior-timeline',1,**{'user_id:sqlstring':"'u'"})),2)

    def test_shallow_player_kept_in_opening_denominator(self):
        self.flow(duration=30);self.flow(user='shallow',run='short',duration=30)
        self.emit(2,'fishing_started',operation_id='op');refresh_behavior(self.db)
        row=self.query('gameplay-behavior-opening',1)[0]
        self.assertEqual(row['全部样本数'],2)

    def test_filters_use_server_session_channel(self):
        self.flow()
        self.db.execute("INSERT INTO analytics_session_channels VALUES ('u','server-r','steam')")
        self.assertEqual(len(self.query('gameplay-behavior-cohorts',1,**{'distribution_channel:sqlstring':"'steam'"})),1)
        self.assertEqual(self.query('gameplay-behavior-cohorts',1,**{'distribution_channel:sqlstring':"'web'"}),[])

    def test_budget_clears_stale_projection_and_preserves_raw(self):
        self.flow();self.emit(601,'journey_checkpoint',totals={'active':600,'ai':600})
        count=self.db.execute('SELECT COUNT(*) FROM analytics_event_facts').fetchone()[0]
        refresh_behavior(self.db,max_points=2)
        self.assertIsNone(self.player())
        self.assertEqual(self.db.execute('SELECT status FROM behavior_projection_status').fetchone()[0],'history_limit')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analytics_event_facts').fetchone()[0],count)

    def test_schema_is_idempotent_and_does_not_commit_caller_transaction(self):
        self.db.commit();self.db.execute('BEGIN');self.db.execute("INSERT INTO behavior_dirty(user_id) VALUES ('rollback')")
        ensure_behavior_schema(self.db);self.db.rollback()
        self.assertIsNone(self.db.execute("SELECT 1 FROM behavior_dirty WHERE user_id='rollback'").fetchone())

    def test_existing_users_backfill_resumes_without_replaying_completed_pages(self):
        self.db.execute('DELETE FROM behavior_migrations')
        for user in ('a', 'b', 'c'):
            self.emit(0, user=user, run=user)
        self.db.execute('DELETE FROM behavior_dirty')
        self.db.commit()
        count, done = backfill_existing_behavior(self.db, batch=2)
        self.assertEqual((count, done), (2, False))
        self.db.commit()
        self.assertEqual([r[0] for r in self.db.execute('SELECT user_id FROM behavior_dirty ORDER BY user_id')], ['a', 'b'])
        count, done = backfill_existing_behavior(self.db, batch=2)
        self.assertEqual((count, done), (1, True))
        self.db.commit()
        self.assertEqual([r[0] for r in self.db.execute('SELECT user_id FROM behavior_dirty ORDER BY user_id')], ['a', 'b', 'c'])
        self.assertEqual(backfill_existing_behavior(self.db, batch=2), (0, True))

    def test_new_event_during_replay_keeps_user_pending(self):
        self.emit(0, 'journey_world_ready')
        from behavior_analytics import reduce_player
        def changed_during_replay(events):
            result = reduce_player(events)
            self.emit(30, 'journey_quit', totals={'active': 30, 'ai': 30})
            return result
        with patch('behavior_analytics.reduce_player', side_effect=changed_during_replay):
            self.assertEqual(refresh_behavior(self.db), 0)
        self.assertEqual(self.db.execute("SELECT pending FROM behavior_dirty WHERE user_id='u'").fetchone()[0], 1)
        self.assertIsNone(self.player())
        self.assertEqual(refresh_behavior(self.db), 1)
        self.assertEqual(self.player()['active'], 30)

    def test_legacy_actions_remain_in_timeline_without_world_clock(self):
        self.emit(60,'fishing_started',operation_id='old-op');refresh_behavior(self.db)
        self.assertIsNone(self.player())
        rows=self.query('gameplay-behavior-timeline',1,**{'user_id:sqlstring':"'u'"})
        self.assertEqual(len(rows),1);self.assertIsNone(rows[0]['入岛后秒'])

    def test_fishing_result_does_not_inflate_cast_count(self):
        self.emit(0,'journey_world_ready')
        self.emit(1,'fishing_started',operation_id='op')
        self.emit(2,'fishing_finished',operation_id='op',outcome='success')
        self.emit(2,'fishing_catch',fish={'fish_name':'测试鱼'})
        refresh_behavior(self.db)
        self.assertEqual(self.query('gameplay-behavior-opening',1)[0]['动作次数'],1)

    def test_later_old_client_behavior_is_unknown_not_zero_playtime(self):
        self.flow();self.emit(90000,'fishing_started',run='legacy',operation_id='op')
        refresh_behavior(self.db)
        self.assertIsNone(self.query('gameplay-behavior-cohorts',1)[0]['后续分钟P50'])

    def test_existing_history_does_not_become_new_early_profile(self):
        self.emit(-86400,'fishing_started',run='old')
        self.flow()
        self.assertEqual(self.player()['quality'],'missing_early')
        self.assertEqual(self.player()['preference'],'insufficient')

    def test_outcome_active_days_clips_cross_day_intervals(self):
        self.flow()
        self.emit(86400-10,'journey_world_ready',run='boundary')
        self.emit(86400+20,'journey_quit',run='boundary',totals={'active':30,'ai':30})
        self.flow(offset=86400+60,run='same-day',duration=30)
        refresh_behavior(self.db)
        row=self.query('gameplay-behavior-cohorts',1)[0]
        self.assertEqual(row['后续活跃天数均值'],1)

    def test_late_anonymous_creation_links_only_its_authenticated_run(self):
        self.emit(10,'journey_world_ready')
        self.emit(40,'journey_quit',totals={'active':30,'ai':30})
        refresh_behavior(self.db)
        self.emit(1,'journey_node_ended',user='anon:device',node_id='player_create',status='completed')
        # Delivery order differs; preserve the event's original sequence before world entry.
        self.db.execute("UPDATE analytics_event_facts SET sequence=0 WHERE event_id=?",(str(self.ids),))
        self.db.commit()
        refresh_behavior(self.db)
        self.assertEqual(self.player()['cohort_kind'],'new_observed')
        self.assertIsNone(self.player('anon:device'))

    def test_invalid_optional_payload_does_not_block_importer(self):
        self.emit(0,'journey_world_ready',participation='invalid')
        self.emit(1,'fishing_catch',fish='invalid',item_name={'bad':'type'})
        refresh_behavior(self.db)
        self.assertEqual(self.player()['quality'],'missing_early')

    def test_timeline_updates_before_background_portrait_replay(self):
        self.emit(1,'fishing_started',operation_id='fresh')
        self.assertIsNone(self.player())
        rows=self.query('gameplay-behavior-timeline',1,**{'user_id:sqlstring':"'u'"})
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['operation_id'],'fresh')

    def test_editor_return_does_not_inflate_production_retention(self):
        self.flow();self.flow(run='editor-return',offset=7*86400+60,duration=30)
        self.db.execute("UPDATE analytics_event_facts SET client_platform='editor',is_development_build=1 WHERE session_id='editor-return'")
        row=self.query('gameplay-behavior-cohorts',1,**{'test_data':'exclude'})[0]
        self.assertEqual(row['D7主动回访率'],0)
        row=self.query('gameplay-behavior-cohorts',1,**{'test_data':'include'})[0]
        self.assertEqual(row['D7主动回访率'],100)

    def test_backwards_future_clock_is_unknown(self):
        self.flow();self.emit(91000,'journey_world_ready',run='backwards')
        self.emit(90000,'journey_quit',run='backwards',totals={'active':30,'ai':30})
        refresh_behavior(self.db)
        self.assertIsNone(self.query('gameplay-behavior-cohorts',1)[0]['后续分钟P50'])

    def test_preference_thresholds_and_cat_affinity_are_separate(self):
        cases=[({'active':600,'ai':300,'fishing':300},'mixed'),
               ({'active':1000,'farming':600,'other':400},'simulation'),
               ({'active':600,'cat':600},'other'),
               ({'active':299,'ai':299},'insufficient')]
        for values,expected in cases:
            totals=dict.fromkeys(METRICS,0.0);totals.update(values)
            self.assertEqual(profile(totals,'observed')[0],expected)

    def test_idle_is_independent_of_preference(self):
        totals=dict.fromkeys(METRICS,0.0);totals.update(active=600,ai=600,idle=2000)
        self.assertEqual(profile(totals,'observed'),('ai','idle'))
        self.assertEqual(profile(totals,'incomplete'),('insufficient','insufficient'))

    def test_tail_without_quit_is_incomplete(self):
        self.emit(0,'journey_world_ready');self.emit(30,totals={'active':30,'ai':30})
        refresh_behavior(self.db);self.assertEqual(self.player()['quality'],'incomplete')


if __name__ == '__main__': unittest.main()
