import json
import unittest
from tests.test_telemetry_analytics import AnalyticsTests
from generate_analytics_dashboards import build
from telemetry_events import accept_batch
from playtime_store import record_play_session_event
from import_gameplay_telemetry import import_sample


class PlayerAnalyticsTests(unittest.TestCase):
    setUp = AnalyticsTests.setUp
    tearDown = AnalyticsTests.tearDown

    def emit(self,user,kind,payload,day=1,archive='island-a',eid=None):
        count=self.db.execute('SELECT COUNT(*) FROM analytics_event_facts').fetchone()[0]
        event={'event_id':eid or f'e{count}','sequence':count,'schema_version':3,'event_type':kind,
               'event_real_time_iso':f'2026-09-23T10:{count//60:02}:{count%60:02}Z','archive_id':archive,
               'event_game_day':day,'actor':{'agent_id':user,'is_player':True},'payload':payload}
        accept_batch(self.path,{'user_id':user,'session_id':'s-'+user,'client_platform':'windows','events':[event]}, {})
        return event

    def query(self,board,pid,user='',**overrides):
        dashboard=build()[board+'.json'];p=next(p for p in dashboard['panels'] if p['id']==pid)
        sql=p['targets'][0]['queryText']
        values={'client_platform:sqlstring':"'webgl','windows','unknown'",'distribution_channel:sqlstring':"'__all__'",'release_version:sqlstring':"'__all__'",'test_data':'auto','playtest_id':'all',
                'user_id:sqlstring':"'"+user+"'" if user else '', 'session_id:sqlstring':'','llm_request_id:sqlstring':'',
                'page:sqlstring':"'0'",'behavior_period':'all','sort_field':'latest_login','sort_direction':'desc',
                'event_type:sqlstring':'','theater_event_id:sqlstring':'','min_invitations:sqlstring':"'10'",'__from':'0','__to':'2000000000000'}
        values.update(overrides)
        for k,v in values.items():sql=sql.replace('${'+k+'}',v)
        return self.db.execute(sql).fetchall()

    def snapshot(self,user,money,day=1,archive='island-a',cats=None):
        return self.emit(user,'player_state_snapshot',{'snapshot_version':1,'nickname':user,'money':money,'island_level':2,
            'cats':cats or [],'careers':{'渔夫':2},'outfits':[{'slot_id':25,'slot':'耳环','item_id':1087,'item_name':'珍珠耳环'}],
            'cat_house_count':1,'cat_house_capacity':5,'cat_house_occupied':0},day,archive)

    def test_latest_balance_zero_is_real_not_missing_and_median_per_player(self):
        self.snapshot('a',100);self.snapshot('a',0);self.snapshot('b',90)
        self.assertEqual(tuple(self.query('gameplay-overview',101)[0]),(90,2,0,2,2))
        self.assertEqual(tuple(self.query('gameplay-overview',104)[0]),(2,2,2))
        self.assertEqual(self.query('gameplay-overview',152)[0][0:2],('耳环','珍珠耳环'))

    def test_snapshot_coverage_preserves_missing_players_and_real_zero(self):
        self.snapshot('a',0)
        self.emit('b','fishing_catch',{})
        rows=self.query('gameplay-overview',155)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0][2:5],(2,1,1))
        self.assertEqual(self.query('gameplay-overview',101)[0][0],0)

    def test_days_deduplicate_same_day_and_separate_archives(self):
        self.snapshot('a',0,day=1);self.snapshot('a',0,day=1);self.snapshot('a',0,day=2)
        self.snapshot('a',0,day=1,archive='island-b')
        self.assertEqual(self.query('gameplay-player-detail',100,'a')[0][3],3)
        self.assertEqual(len(self.query('gameplay-player-days',100,'a')),3)
        self.assertEqual(self.query('gameplay-player-detail',100),[])

    def test_actual_stamina_does_not_count_two_business_events_or_miss_failed_fishing(self):
        self.emit('a','building_placed',{'building_name':'农田'})
        self.emit('a','farming_till',{})
        self.emit('a','stamina_spent',{'action':'farming_till','amount':3})
        self.emit('a','stamina_spent',{'action':'fishing','amount':8})
        self.emit('a','fishing_finished',{'region':'WaterPond','water_kind':'shadow','outcome':'failed'})
        rows=self.query('gameplay-overview',141)
        self.assertEqual([tuple(r) for r in rows],[('fishing',8,1,0,1,8),('farming_till',3,1,0,1,3)])
        self.assertEqual(tuple(self.query('gameplay-overview',142)[0]),('WaterPond','shadow',1,0,1,0))

    def test_skill_points_exclude_reward_and_freeze_counts_only_transitions(self):
        self.emit('a','cat_level_up',{'cat_uid':0,'remaining_before_reward':0,'skill_points':1})
        self.emit('a','cat_level_up',{'cat_uid':0,'remaining_before_reward':2,'skill_points':3})
        self.assertEqual(tuple(self.query('gameplay-overview',144)[0]),(1,50,2))
        for locked in (True,False,True):self.emit('a','shop_freeze_changed',{'item_name':'衣服','shop_type':'clothing','frozen':locked})
        self.assertEqual(self.query('gameplay-overview',124)[0][1],2)

    def test_raw_envelope_survives_final_snapshot_and_duplicate_files(self):
        event=self.emit('a','stamina_spent',{'action':'fishing','amount':2},eid='canonical')
        sample={'user_id':'a','client_platform':'windows','gameplay_telemetry':{'session_meta':{'session_id':'s-a'},'days':{'1':{'events':[event]}}}}
        for filename in ('snapshot1','snapshot2'):
            import_sample(self.db,source_file=filename,sample=sample,imported_at='2026-09-23T11:00:00Z')
        self.db.commit()
        rows=self.query('gameplay-player-events',100,'a')
        self.assertEqual(len(rows),1)
        metadata=json.loads(rows[0][6]);self.assertEqual(metadata['archive_id'],'island-a')
        self.assertEqual(metadata['actor']['is_player'],True)
        self.assertEqual(json.loads(rows[0][7])['amount'],2)

    def test_sort_is_global_before_paging_and_empty_textboxes_are_valid(self):
        self.snapshot('a',1);self.snapshot('b',2)
        self.assertEqual(self.query('gameplay-players',100,sort_field='money')[0][1],'b')
        self.assertEqual(self.query('gameplay-players',100,sort_field='money',sort_direction='asc')[0][1],'a')
        self.assertEqual(self.query('gameplay-players',100,**{'page:sqlstring':"'15'"}),[])
        for board in ('gameplay-player-detail','gameplay-player-days','gameplay-player-events'):
            self.assertEqual(self.query(board,100),[])

    def test_version_filter_does_not_reclassify_existing_player_as_new(self):
        for session,version,date in [('old','1','2026-01-01T10:00:00Z'),('new','2','2026-09-23T10:00:00Z')]:
            payload={'user_id':'a','session_id':session,'client_platform':'windows','client_version':version,'timestamp':date}
            record_play_session_event(self.db,payload=payload,headers={},event_type='login',received_at=date)
        self.db.commit()
        self.snapshot('a',5)
        self.assertTrue(self.query('gameplay-player-detail',100,'a')[0][5].startswith('2026-01-01'))

    def test_current_dish_field_and_legacy_till_energy(self):
        self.emit('a','cooking_completed',{'recipe_id':1,'dish_name':'蘑菇汤'})
        self.assertEqual(self.query('gameplay-overview',127)[0][0],'蘑菇汤')
        events=[]
        for i,kind in enumerate(('building_placed','farming_till')):
            events.append({'event_id':f'legacy-till-{i}','schema_version':2,'event_type':kind,'sequence':i,'energy_cost':3,
                'event_game_day':1,'actor':{'agent_id':'a','is_player':True},
                'payload':{'building_id':6001,'position_x':4,'position_y':5}})
        import_sample(self.db,source_file='legacy-till',sample={'user_id':'a','client_platform':'windows','gameplay_telemetry':
            {'session_meta':{'session_id':'s-a'},'days':{'1':{'events':events}}}},imported_at='2026-09-23T10:00:00Z')
        self.db.commit()
        self.assertEqual([tuple(r) for r in self.query('gameplay-overview',141)],[('farming_till',3,1,1,1,3)])

    def test_overview_does_not_inherit_selected_player(self):
        self.snapshot('a',1);self.snapshot('b',2)
        self.assertEqual(self.query('gameplay-overview',101,'a')[0][0],3)

    def test_live_backfill_normalizes_release_and_preserves_full_version(self):
        from analytics_facts import migrate_facts
        payload={'user_id':'a','session_id':'s','client_platform':'windows','client_version':'unity-demo-v1.1.2.3.abcdef0','events':[AnalyticsTests.event(self)]}
        accept_batch(self.path,payload,{})
        self.db.execute('DELETE FROM analytics_event_facts')
        self.db.execute('DELETE FROM analytics_migrations')
        self.db.commit()
        migrate_facts(self.db)
        self.assertEqual(self.db.execute('SELECT client_version,release_version FROM analytics_event_facts').fetchone(),('unity-demo-v1.1.2.3.abcdef0','demo-v1'))

    def test_restored_ai_reports_deduplicate_snapshots_and_do_not_invent_call_dates(self):
        for source in ('first','retry'):
            self.db.execute("""INSERT INTO gameplay_sessions(source_file,user_id,session_id,imported_at,real_time_started_iso,client_platform,client_version,release_version,ai_response_count,ai_total_tokens,ai_input_tokens,ai_output_tokens,ai_cached_input_tokens,ai_estimated_cost_usd)
                VALUES (?,'a','s','2026-09-23','2026-09-23T00:00:00Z','windows','v1','v1',2,1100,1000,100,200,0.5)""",(source,))
            self.db.execute("""INSERT INTO gameplay_ai_calls(source_file,user_id,session_id,event_index,game_day,imported_at,client_platform,client_version,release_version,model,total_tokens,input_tokens,output_tokens,cached_input_tokens,estimated_cost_usd)
                VALUES (?,'a','s',1,0,'2026-09-23','windows','v1','v1','test-model',1100,1000,100,200,0.5)""",(source,))
        self.db.commit()
        for pid,expected in ((20,2),(21,1100),(22,0.5),(23,20)):
            self.assertEqual(self.query('gameplay-overview',pid)[0][0],expected)
        self.assertEqual(self.query('gameplay-overview',26)[0][2],1)
        daily=self.query('gameplay-overview',27)
        self.assertEqual(daily[0][0],'未采集日期')
        self.assertEqual(daily[0][1],1)
        self.assertEqual(self.query('gameplay-overview',27,behavior_period='range'),[])
        self.assertEqual(self.query('gameplay-overview',20,**{'client_platform:sqlstring':"'editor'"})[0][0],0)

    def test_restored_tasks_manual_chat_and_player_columns_respect_origin(self):
        self.db.execute("INSERT INTO user_sessions(user_id,nickname,tasks_completed,tasks_total,current_task_title,current_task_status) VALUES ('a','名字',2,5,'种植','active')")
        payload={'user_id':'a','session_id':'s','client_platform':'windows','client_version':'v1','timestamp':'2026-09-23T00:00:00Z'}
        record_play_session_event(self.db,payload=payload,headers={},event_type='login',received_at=payload['timestamp'])
        for kind,preset,platform in [('chat',0,'windows'),('chat',1,'windows'),('chat',0,'editor'),('login',0,'windows')]:
            self.db.execute("INSERT INTO conversations(user_id,session_id,message_type,user_query,is_preset,client_platform,timestamp,client_version) VALUES ('a','s',?,'文本',?,?,'2026-09-23T01:00:00Z','v1')",(kind,preset,platform))
        self.db.commit()
        listing=self.query('gameplay-players',100)[0]
        self.assertEqual(listing[11:16],('v1',1,'2/5','种植','active'))
        self.assertEqual(listing[18],'2026-09-23 09:00:00')
        self.assertEqual(tuple(self.query('gameplay-overview',28)[0]),('种植','active',1,1))
        self.assertEqual(self.query('gameplay-overview',29)[0][2],1)

    def test_playtest_filter_separates_players_and_event_details(self):
        self.db.execute("INSERT INTO analytics_session_playtests VALUES ('a','s-a','中秋playtest')")
        self.db.commit()
        self.snapshot('a',20)
        self.snapshot('b',80)
        self.assertEqual(self.query('gameplay-overview',101,playtest_id='中秋playtest')[0][0],20)
        self.assertEqual(self.query('gameplay-overview',101,playtest_id='legacy')[0][0],80)
        self.assertEqual(self.query('gameplay-overview',101,playtest_id='all')[0][0],100)
        self.assertEqual(self.query('gameplay-player-events',100,'b',playtest_id='中秋playtest'),[])
