import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path

from analytics_facts import ensure_facts,upsert_fact
from journey_analytics import backfill_existing_journeys,ensure_journey_schema,refresh_journeys,project,METRICS,stamp
from generate_analytics_dashboards import build

BASE=datetime(2026,9,1,tzinfo=timezone.utc)


class JourneyTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');ensure_facts(self.db)
        self.db.executescript('''CREATE TABLE user_sessions(user_id TEXT,is_developer INTEGER);
          CREATE TABLE play_session_events(user_id TEXT,client_sent_at TEXT,received_at TEXT,event_type TEXT,app_state TEXT);
          CREATE TABLE analytics_session_channels(user_id TEXT,session_id TEXT,distribution_channel TEXT);
          CREATE TABLE analytics_session_playtests(user_id TEXT,session_id TEXT,playtest_id TEXT);''')
        self.seq=0

    def emit(self,kind,t,eff=None,run='r',user='u',archive='a',**payload):
        self.seq+=1;eff=t if eff is None else eff
        p={m+'_seconds':0 for m in METRICS}
        p.update(effective_seconds=eff,foreground_seconds=eff,single_seconds=eff,
                 mode='single',humans=1,current_node='world',flow_version='journey-v1')
        p.update(payload)
        e={'event_id':str(self.seq),'sequence':self.seq,'event_real_time_iso':(BASE+timedelta(seconds=t)).isoformat(),
           'archive_id':archive,'event_type':kind,'payload':p}
        upsert_fact(self.db,e,user,run,{'client_platform':'windows'},e['event_real_time_iso'],version='unity-test',release='test')
        return e

    def flow(self):
        self.emit('journey_started',0,archive='',current_node='startup',user='anon:x')
        self.emit('journey_context',1,tasks=[{'task_id':3508,'state':0,'value':0,'target_value':1}],island_level=1)
        self.emit('journey_node_started',2,node_id='player_create',current_node='player_create')
        self.emit('journey_node_ended',12,node_id='player_create',status='completed',current_node='player_create')
        self.emit('journey_node_started',12,node_id='cat_create',current_node='cat_create')
        self.emit('journey_node_ended',32,node_id='cat_create',status='completed',current_node='cat_create')
        self.emit('journey_world_ready',32,island_level=1)

    def project(self):
        self.db.commit()
        refresh_journeys(self.db)

    def node(self,nid):
        self.db.row_factory=sqlite3.Row
        r=self.db.execute('SELECT * FROM journey_nodes WHERE node_id=?',(nid,)).fetchone()
        self.db.row_factory=None
        return dict(r) if r else None

    def query(self,board,pid,user='',**kw):
        sql=next(p for p in build()[board+'.json']['panels'] if p['id']==pid)['targets'][0]['queryText']
        values={'client_platform:sqlstring':"'__all__'",'distribution_channel:sqlstring':"'__all__'",'release_version:sqlstring':"'__all__'",
                'test_data':'include','playtest_id':'all','user_id:sqlstring':"'"+user+"'",'node_id:sqlstring':"''",
                'churn_days':'7','cohort_kind':'all','mode_group':'all','flow_version':'journey-v1','observed_until:sqlstring':"'2026-09-10T00:00:00Z'",'__from':'0','__to':'2000000000000'}
        values.update(kw)
        for k,v in values.items():sql=sql.replace('${'+k+'}',v)
        sql=sql.replace("julianday('now')","julianday("+kw.get("observed_until:sqlstring", "'2026-09-10T00:00:00Z'")+")")
        self.assertNotIn('${',sql)
        cursor=self.db.execute(sql);cols=[c[0] for c in cursor.description]
        return [dict(zip(cols,r)) for r in cursor]

    def test_identity_dedup_and_exact_node_time(self):
        self.flow()
        e=self.emit('journey_business',62,business_type='task_completed',business={'task_id':3508,'value':1})
        upsert_fact(self.db,e,'u','r',{'client_platform':'windows'},e['event_real_time_iso'])
        self.project()
        self.assertEqual(self.db.execute('SELECT user_id FROM journey_players').fetchall(),[('u',)])
        self.assertEqual(self.node('player_create')['effective'],10)
        self.assertEqual(self.node('cat_create')['effective'],20)
        self.assertEqual(self.node('task:3508')['effective'],30)
        self.assertEqual(self.node('task:3508')['world_effective'],30)
        self.assertEqual(self.node('task:3508')['cumulative_effective'],62)
        before=self.db.execute('SELECT * FROM journey_nodes ORDER BY node_id').fetchall()
        self.db.commit()
        refresh_journeys(self.db);self.assertEqual(before,self.db.execute('SELECT * FROM journey_nodes ORDER BY node_id').fetchall())

    def test_cross_session_does_not_count_offline(self):
        self.flow();self.emit('journey_quit',62)
        self.emit('journey_started',86400,eff=0,run='r2')
        self.emit('journey_context',86400,eff=0,run='r2',tasks=[{'task_id':3508,'state':0}])
        self.emit('journey_business',86430,eff=30,run='r2',business_type='task_completed',business={'task_id':3508})
        self.project();n=self.node('task:3508')
        self.assertEqual(n['effective'],60)
        self.assertEqual(n['natural_seconds'],86430-32)

    def test_multiplayer_shared_upgrade_and_existing_level(self):
        self.flow();self.emit('journey_context',32,mode='multiplayer',humans=1)
        self.emit('journey_checkpoint',62,mode='multiplayer',humans=1,single_seconds=32,multiplayer_alone_seconds=30)
        self.emit('journey_context',62,mode='multiplayer',humans=2,single_seconds=32,multiplayer_alone_seconds=30)
        self.emit('journey_business',92,mode='multiplayer',humans=2,single_seconds=32,multiplayer_alone_seconds=30,multiplayer_together_seconds=30,
                  business_type='island_level_up',submitted_by='friend',business={'after_level':2})
        self.project();n=self.node('level:2')
        self.assertEqual(n['effective'],60);self.assertEqual(n['multiplayer_alone'],30);self.assertEqual(n['multiplayer_together'],30)
        self.assertEqual(self.db.execute('SELECT mode_group FROM journey_players').fetchone()[0],'mixed')
        self.emit('journey_context',0,eff=0,run='visitor',user='visitor',island_level=8,tasks=[])
        self.project()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM journey_nodes WHERE user_id='visitor' AND status='joined_existing' AND effective IS NULL").fetchone()[0],7)

    def test_every_dashboard_sql_executes_and_mature_churn(self):
        self.flow();self.emit('journey_quit',62);self.project()
        for board in ('gameplay-journey-flow','gameplay-journey-loss','gameplay-journey-modes'):
            for panel in build()[board+".json"]["panels"]:
                if panel.get("targets"): self.query(board,panel["id"])
        row=next(r for r in self.query('gameplay-journey-flow',1) if r['node_id']=='task:3508')
        self.assertEqual(row['流失人数'],1);self.assertEqual(row['流失率分母'],1);self.assertEqual(row['流失率'],100)
        self.db.execute("INSERT INTO play_session_events VALUES ('u','2026-09-09T00:00:00Z',NULL,'login','foreground')")
        row=next(r for r in self.query('gameplay-journey-flow',1) if r['node_id']=='task:3508')
        self.assertEqual(row['流失人数'],0)

    def test_young_cohort_is_observing_not_churn(self):
        self.flow();self.emit('journey_quit',62);self.project()
        row=next(r for r in self.query('gameplay-journey-flow',1,**{'observed_until:sqlstring':"'2026-09-03T00:00:00Z'"}) if r['node_id']=='task:3508')
        self.assertEqual(row['流失人数'],0);self.assertEqual(row['观察中'],1);self.assertIsNone(row['流失率'])

    def test_late_delivery_rebuilds_missing_start(self):
        self.emit('journey_business',60,business_type='task_completed',business={'task_id':3508})
        self.project();self.assertIsNone(self.node('task:3508')['effective'])
        self.emit('journey_business',30,business_type='task_started',business={'task_id':3508})
        self.project();self.assertEqual(self.node('task:3508')['effective'],30)

    def test_gap_never_becomes_zero_duration_success(self):
        self.flow();self.emit('journey_business',1000,business_type='task_completed',business={'task_id':3508})
        self.project();self.assertEqual(self.node('task:3508')['quality'],'incomplete');self.assertIsNone(self.node('task:3508')['effective'])

    def test_background_checkpoints_are_not_return_visits(self):
        self.flow();self.emit('journey_background',62,app_state='background')
        for t in (92,122,152):self.emit('journey_checkpoint',t,eff=62,app_state='background')
        self.project()
        self.assertEqual(self.db.execute('SELECT last_at FROM journey_players').fetchone()[0],(BASE+timedelta(seconds=62)).isoformat())
        row=next(r for r in self.query('gameplay-journey-flow',1) if r['node_id']=='task:3508')
        self.assertEqual(row['流失人数'],1)

    def test_claim_wait_is_a_separate_exit_node(self):
        self.flow();self.emit('journey_business',62,business_type='task_completed',business={'task_id':3508})
        self.emit('journey_quit',82);self.project()
        self.assertEqual(self.db.execute('SELECT node_id FROM journey_exits WHERE is_final=1').fetchone()[0],'task:3508:claim')
        task=next(r for r in self.query('gameplay-journey-flow',1) if r['node_id']=='task:3508')
        claim=next(r for r in self.query('gameplay-journey-flow',1) if r['node_id']=='task:3508:claim')
        self.assertEqual(task['流失人数'],0);self.assertEqual(claim['流失人数'],1)
        self.assertEqual(self.node('task:3508:claim')['effective'],20)

    def test_concurrent_clients_do_not_double_count(self):
        self.emit('journey_started',0,eff=0,run='r1')
        self.emit('journey_started',10,eff=0,run='r2')
        self.emit('journey_quit',30,eff=30,run='r1')
        self.emit('journey_quit',40,eff=30,run='r2')
        self.project()
        seconds,quality=self.db.execute('SELECT effective,quality FROM journey_players').fetchone()
        self.assertEqual(seconds,30);self.assertEqual(quality,'incomplete')

    def test_version_and_platform_filter_do_not_hide_return(self):
        self.flow();self.emit('journey_quit',62);self.project()
        self.db.execute("INSERT INTO play_session_events VALUES ('u','2026-09-09T00:00:00Z',NULL,'heartbeat','foreground')")
        row=next(r for r in self.query('gameplay-journey-flow',1,**{'client_platform:sqlstring':"'windows'",'release_version:sqlstring':"'test'"}) if r['node_id']=='task:3508')
        self.assertEqual(row['流失人数'],0)

    def test_all_23_tasks_and_levels_2_through_8(self):
        from journey_analytics import TASKS
        self.flow();t=32
        for tid in TASKS:
            self.emit('journey_business',t,business_type='task_started',business={'task_id':int(tid)})
            t+=30;self.emit('journey_business',t,business_type='task_completed',business={'task_id':int(tid)})
            t+=5;self.emit('journey_business',t,business_type='task_claimed',business={'task_id':int(tid)})
        for level in range(2,9):
            t+=60;self.emit('journey_business',t,business_type='island_level_up',business={'after_level':level})
        self.emit('journey_quit',t);self.project()
        for tid in TASKS: self.assertEqual(self.node('task:'+tid)['effective'],30)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM journey_nodes WHERE node_id LIKE 'level:%' AND completed_at IS NOT NULL").fetchone()[0],7)
        self.assertEqual(self.db.execute('SELECT node_id FROM journey_exits WHERE is_final=1').fetchone()[0],'mainline_complete')

    def test_level_dropout_is_current_level_not_last_task(self):
        self.flow();self.emit('journey_business',62,business_type='island_level_up',business={'after_level':2})
        self.emit('journey_quit',92);self.project()
        row=next(r for r in self.query('gameplay-journey-modes',2) if r['node_id']=='level:3')
        self.assertEqual(row['流失人数'],1)

    def test_menu_heartbeat_does_not_erase_last_game_node(self):
        self.flow();self.emit('journey_leave',62)
        self.emit('journey_checkpoint',92,archive='',current_node='menu')
        self.project()
        node,at=self.db.execute('SELECT node_id,occurred_at FROM journey_exits WHERE is_final=1').fetchone()
        self.assertEqual(node,'task:3508');self.assertEqual(stamp(at),stamp(BASE.isoformat())+62)

    def test_loading_spans_prelogin_archive_binding(self):
        self.emit('journey_node_started',0,archive='',node_id='loading',node_archive_id='')
        self.emit('journey_context',10)
        self.emit('journey_node_ended',30,node_id='loading',node_archive_id='',status='completed')
        self.project();self.assertEqual(self.node('loading')['effective'],30)

    def test_dotnet_roundtrip_timestamp_is_accepted_without_losing_timezone(self):
        from telemetry_time import parse_timestamp
        self.assertEqual(parse_timestamp('2026-09-24T17:56:52.1346420+00:00').microsecond,134642)
        self.assertEqual(parse_timestamp('2026-09-24T17:56:52.1346420Z').utcoffset().total_seconds(),0)
        with self.assertRaises(ValueError):parse_timestamp('2026-09-24T17:56:52')

    def test_limit_is_explicit_and_intake_is_bounded(self):
        self.flow();self.db.commit();refresh_journeys(self.db,max_points=2)
        self.assertEqual(self.db.execute('SELECT status FROM journey_projection_status WHERE user_id=?',('u',)).fetchone()[0],'history_limit')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journey_players').fetchone()[0],0)

    def test_existing_facts_are_queued_once_for_projection(self):
        self.flow()
        self.db.execute('DELETE FROM journey_run_owners')
        self.db.execute('DELETE FROM journey_dirty')
        self.assertEqual(backfill_existing_journeys(self.db),1)
        self.assertEqual(self.db.execute('SELECT user_id FROM journey_run_owners WHERE run_id=?',('r',)).fetchone(),('u',))
        self.assertEqual(self.db.execute('SELECT user_id FROM journey_dirty').fetchone(),('u',))
        self.assertEqual(backfill_existing_journeys(self.db),0)
        self.db.commit()
        refresh_journeys(self.db)
        self.assertEqual(self.db.execute('SELECT user_id FROM journey_players').fetchone(),('u',))

    def test_ready_schema_does_not_repeat_catalog_writes(self):
        statements=[]
        self.db.set_trace_callback(statements.append)
        ensure_journey_schema(self.db)
        self.assertFalse(any(statement.lstrip().upper().startswith(('CREATE ','ALTER ','INSERT ')) for statement in statements))


if __name__=='__main__':unittest.main()
