"""Identity and missing-data regressions for the AI feature dashboards."""
import unittest
from tests import test_player_analytics as helpers


class BusinessAnalyticsTests(unittest.TestCase):
    setUp=helpers.PlayerAnalyticsTests.setUp
    tearDown=helpers.PlayerAnalyticsTests.tearDown
    emit=helpers.PlayerAnalyticsTests.emit
    query=helpers.PlayerAnalyticsTests.query

    def test_old_client_has_no_fabricated_lifecycle_or_actor_count(self):
        self.emit('a','theater_line',{'theater_event_id':'old','theater_type':'pair','mounted_cats_count':3})
        summary=self.query('gameplay-overview',301)[0]
        self.assertIsNone(summary[0]);self.assertIsNone(summary[2])
        self.assertEqual(summary[1],1)
        breakdown=self.query('gameplay-overview',302)[0]
        self.assertEqual(breakdown[2],'未采集')
        self.assertIsNone(breakdown[4]);self.assertIsNone(breakdown[6])

    def test_multiplayer_lines_are_one_scene_and_uid_zero_is_in_actor_count(self):
        self.emit('a','theater_lifecycle',{'flow_id':'f','theater_type':'pair','phase':'offered','participant_count':2})
        for user in ('a','b'):
            for _ in range(2):
                self.emit(user,'theater_line',{'flow_id':'f','theater_type':'pair','participant_count':2,'participant_pet_uids':[0,1]})
        self.emit('a','theater_lifecycle',{'flow_id':'f','theater_type':'pair','phase':'completed'})
        row=self.query('gameplay-overview',301)[0]
        self.assertEqual(row[:3],(1,1,1));self.assertEqual(row[-1],100)

    def test_generation_completion_and_placement_are_not_two_generations(self):
        for phase in ('requested','completed'):
            self.emit('a','ai_building_generation',{'flow_id':'g','phase':phase,'source_id':'asset'})
        self.emit('a','ai_generation_completed',{'status':'placed'})
        self.emit('a','ai_building_created',{'building_instance_id':0,'source_id':'asset','money_spent':20})
        row=self.query('gameplay-overview',308)[0]
        self.assertEqual(row[:3],(1,1,1));self.assertEqual(row[-2:],(1,20))
        self.assertEqual(self.query('gameplay-overview',317)[0],(1,1,100))

    def test_adventure_pause_resume_does_not_increase_adventures(self):
        for revision,state in enumerate(('inviting','playing_act_1','paused','gathering','playing_act_1','completed')):
            self.emit('a','ai_adventure_state',{'flow_id':'adventure','phase':state,'revision':revision,'participant_count':2,'theme_name':'森林'})
        rows=self.query('gameplay-overview',311)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0][3:5],('completed',1))

    def test_request_lifecycle_cost_is_counted_once_and_missing_is_reported(self):
        for phase in ('requested','completed'):
            self.emit('a','ai_business_request',{'request_id':'r','business_group':'adventure','phase':phase,'estimated_usd':.2 if phase=='completed' else None})
        self.emit('a','ai_business_request',{'request_id':'unknown','business_group':'adventure','phase':'completed'})
        row=self.query('gameplay-overview',314)[0]
        self.assertEqual(row[1:3],(2,2));self.assertAlmostEqual(row[6],.2);self.assertEqual(row[7],1)
        self.assertEqual(self.query('gameplay-player-detail',314),[])

    def test_resuming_old_scene_does_not_move_it_to_new_playtest(self):
        self.emit('a','theater_lifecycle',{'flow_id':'f','phase':'offered'})
        self.emit('a','theater_line',{'flow_id':'f'})
        self.emit('a','theater_lifecycle',{'flow_id':'f','phase':'completed'})
        self.db.execute("UPDATE analytics_event_facts SET session_id='new' WHERE event_type='theater_line' OR json_extract(payload_json,'$.phase')='completed'")
        self.db.execute("INSERT INTO analytics_session_playtests VALUES ('a','new','中秋playtest')")
        self.db.commit()
        self.assertEqual(self.query('gameplay-overview',301,playtest_id='中秋playtest')[0][:3],(0,0,0))
        self.assertEqual(self.query('gameplay-overview',301,playtest_id='legacy')[0][:3],(1,1,1))
