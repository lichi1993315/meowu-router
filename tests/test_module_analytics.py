import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from analytics_facts import ensure_facts, upsert_fact
from generate_analytics_dashboards import build
from module_analytics import MODULES, TIME_COLUMNS

BASE = datetime(2026, 11, 1, 2, tzinfo=timezone.utc)
BOARD = 'gameplay-module-engagement'


class ModuleAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:'); ensure_facts(self.db)
        self.db.executescript('''CREATE TABLE user_sessions(user_id TEXT,is_developer INTEGER);
            CREATE TABLE analytics_session_channels(user_id TEXT,session_id TEXT,distribution_channel TEXT);
            CREATE TABLE analytics_session_playtests(user_id TEXT,session_id TEXT,playtest_id TEXT);''')
        self.ids = 0

    def _put(self, event, user, session):
        self.ids += 1
        event.setdefault('event_id', 'e%d' % self.ids)
        event.setdefault('sequence', self.ids)
        upsert_fact(self.db, event, user, session, {'client_platform': 'windows'}, event['event_real_time_iso'], release='test')
        self.db.commit()
        return event

    def server(self, seconds, kind, module, user='u', guided=False, phase='closed', since=None, player=True, **payload):
        event = {'event_type': kind, 'schema_version': 3, 'archive_id': 'island',
                 'event_real_time_iso': (BASE + timedelta(seconds=seconds)).isoformat(),
                 'actor': {'agent_id': user, 'is_player': player}, 'module_id': module, 'guided': guided,
                 'restaurant_phase': phase, 'payload': payload}
        if since is not None:
            event['minutes_since_close'] = since
        return self._put(event, user, 'server-' + user)

    def client(self, seconds, kind, user='u', run='r', **payload):
        payload.setdefault('server_session_id', 'server-' + user)
        event = {'event_type': kind, 'schema_version': 4, 'event_real_time_iso': (BASE + timedelta(seconds=seconds)).isoformat(),
                 'actor': {'is_player': True}, 'payload': payload}
        return self._put(event, user, run)

    def checkpoint(self, seconds, monotonic, modules, user='u', run='r', **extra):
        participation = {'version': 'participation-v1', 'monotonic_seconds': monotonic, 'in_world': True,
                         'modules': {'version': 'modules-v1', **modules}}
        return self.client(seconds, extra.pop('kind', 'journey_checkpoint'), user=user, run=run, participation=participation, **extra)

    def query(self, pid, cutoff='2026-11-20T00:00:00Z', **values):
        sql = next(p for p in build()[BOARD + '.json']['panels'] if p['id'] == pid)['targets'][0]['queryText']
        variables = {'client_platform:sqlstring': "'__all__'", 'distribution_channel:sqlstring': "'__all__'",
                     'release_version:sqlstring': "'__all__'", 'test_data': 'include', 'playtest_id': 'all',
                     'user_id:sqlstring': "''", 'session_id:sqlstring': "''", 'page:sqlstring': "'0'",
                     'module_guided': 'exclude', 'offer_module:sqlstring': "'vending'",
                     '__from': str(int(BASE.timestamp() * 1000) - 3600000), '__to': '2000000000000'}
        variables.update(values)
        for key, value in variables.items():
            sql = sql.replace('${' + key + '}', value)
        sql = sql.replace("julianday('now')", "julianday('" + cutoff + "')")
        self.assertNotIn('${', sql)
        cursor = self.db.execute(sql)
        return [dict(zip([c[0] for c in cursor.description], r)) for r in cursor]

    def row(self, rows, column, value):
        return next(r for r in rows if r[column] == value)

    def populate(self):
        # 第 0 天：钓鱼 3 次（1 次引导中），售货机买 1 次被拒 1 次，闭店后 5 分钟采集
        self.server(10, 'fishing_finished', 'fishing', guided=True, result='success')
        self.server(20, 'fishing_finished', 'fishing', result='success')
        self.server(30, 'feature_action', 'vending', action='buy', result='rejected', reason='short', content_id='rod_2')
        self.server(40, 'feature_action', 'vending', action='buy', result='success', content_id='rod_1')
        self.server(50, 'feature_action', 'foraging', action='pickup', result='success', since=5, content_id='3001')
        self.server(55, 'feature_action', 'foraging', action='pickup', result='success', source='cat', since=6)
        self.server(60, 'feature_action', 'restaurant', phase='open', action='serve', result='success')
        self.server(61, 'restaurant_guest_ordered', 'restaurant', phase='open', player=False, recipe_id=3)
        self.server(70, 'currency_delta', 'vending', currency='dew', reason='vending_buy', delta=-60)
        self.server(80, 'offer_shown', 'vending', surface='vending_machine', items=[
            {'content_id': 'rod_1', 'price': 60, 'locked': False}, {'content_id': 'rod_2', 'price': 900, 'locked': True, 'lock_reason': 'island_4'}])
        self.server(90, 'restaurant_session_started', 'restaurant', phase='open', menu=[{'recipe_id': 3, 'count': 4}])
        self.server(95, 'cooking_completed', 'restaurant', phase='open', recipe_id=3, dish_name='刺身', ordered=False)
        # 第 2 天回来再钓鱼
        self.server(2 * 86400, 'fishing_finished', 'fishing', result='success')
        # 客户端：窗口进出与按模块计时
        self.client(35, 'ui_view_opened', module_id='vending', view_id='UI_VendingWin', open_trigger='player_input')
        self.client(45, 'ui_view_closed', module_id='vending', view_id='UI_VendingWin', open_trigger='player_input', effective_s=10, action_count=2)
        self.client(100, 'ui_view_closed', module_id='collection', view_id='UI_CollectionWin', open_trigger='player_input', effective_s=3, action_count=0)
        self.client(101, 'ui_view_closed', module_id='unknown', view_id='UI_SomethingNew', open_trigger='auto', effective_s=1, action_count=0)
        self.checkpoint(30, 30, {'fishing': 20, 'free_roam': 5})
        self.checkpoint(60, 60, {'fishing': 30, 'vending': 10, 'free_roam': 15})
        self.checkpoint(90, 90, {'fishing': 30, 'vending': 10, 'foraging': 20, 'free_roam': 15},
                        kind='journey_quit', view_stack=[{'view_id': 'UI_VendingWin', 'module_id': 'vending'}],
                        last_action={'module_id': 'vending', 'action': 'buy', 'result': 'rejected', 'seconds_ago': 30})

    def test_every_panel_runs_on_empty_and_populated_db(self):
        for populated in (False, True):
            if populated:
                self.populate()
            for p in build()[BOARD + '.json']['panels']:
                if p.get('targets'):
                    self.query(p['id'])

    def test_overview_splits_time_by_module_and_excludes_guided_by_default(self):
        self.populate()
        rows = self.query(1)
        fishing = self.row(rows, '模块', '钓鱼')
        self.assertEqual(fishing['主动分钟'], 0.5)   # 差值还原：20 + 10 秒
        self.assertEqual(fishing['动作次数'], 2)      # 引导中那次被去掉
        self.assertEqual(fishing['人均使用天数'], 2)
        vending = self.row(rows, '模块', '售货机')
        self.assertEqual(vending['被拒率'], 50.0)
        self.assertEqual(vending['打开窗口人数'], 1)
        foraging = self.row(rows, '模块', '采集')
        self.assertEqual(foraging['动作次数'], 1)     # 猫采的不算玩家动作
        self.assertEqual(self.row(self.query(1, module_guided='include'), '模块', '钓鱼')['动作次数'], 3)
        self.assertEqual(sum(r['时长占比'] or 0 for r in rows if r['模块'] != '自由走动'), 100.0)

    def test_liking_reuse_and_one_time_only_count_matured_players(self):
        self.populate()
        rows = self.query(2)
        self.assertEqual(self.row(rows, '模块', '钓鱼')['第2到7天回来再用率'], 100.0)
        self.assertEqual(self.row(rows, '模块', '售货机')['只用一次比例'], 100.0)
        early = self.query(2, cutoff='2026-11-03T00:00:00Z')
        self.assertIsNone(self.row(early, '模块', '钓鱼')['第2到7天回来再用率'])

    def test_exit_point_after_close_and_projection_idempotence(self):
        self.populate()
        exits = self.query(5)
        self.assertEqual(exits[0]['退出时所在界面'], '售货机')
        self.assertEqual(exits[0]['其中是最后一次且已3天未回'], 1)
        last = self.query(6)
        self.assertEqual((last[0]['最后动作模块'], last[0]['结果']), ('售货机', 'rejected'))
        after = self.query(7)
        self.assertEqual([(r['闭店后'], r['模块'], r['动作次数']) for r in after], [('0–10 分钟', '采集', 1)])
        self.assertEqual(self.query(8)[0]['模块'], '餐厅')
        count = self.db.execute('SELECT COUNT(*) FROM module_events').fetchone()[0]
        event = self.db.execute("SELECT event_id FROM module_events LIMIT 1").fetchone()[0]
        self._put({'event_id': event, 'event_type': 'feature_action', 'event_real_time_iso': BASE.isoformat(),
                   'actor': {'is_player': True}, 'module_id': 'fishing', 'payload': {'result': 'success'}}, 'u', 'server-u')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM module_events').fetchone()[0], count)

    def test_offers_dishes_currency_views_and_quality(self):
        self.populate()
        offers = self.query(10)
        rod1 = self.row(offers, '商品', 'rod_1'); rod2 = self.row(offers, '商品', 'rod_2')
        self.assertEqual((rod1['购买人数'], rod1['看过的人里买了的比例']), (1, 100.0))
        self.assertEqual((rod2['曝光时买不了次数'], rod2['买不了的原因']), (1, 'island_4'))
        dish = self.query(9)[0]
        self.assertEqual((dish['菜名'], dish['开店上架次数'], dish['没人点也做的份数']), ('刺身', 1, 1))
        self.assertEqual(self.query(11)[0]['消耗'], 60)
        views = self.query(3)
        self.assertEqual(self.row(views, '窗口', 'UI_CollectionWin')['点开后没操作就关比例'], 100.0)
        quality = self.query(13)
        self.assertEqual(quality[0]['模块'], '未登记')
        self.assertIn('UI_SomethingNew', quality[0]['未登记来源'])

    def test_old_clients_without_module_stamp_are_not_projected(self):
        self._put({'event_type': 'fishing_catch', 'event_real_time_iso': BASE.isoformat(), 'actor': {'is_player': True},
                   'payload': {'fish_id': 1}}, 'old', 's')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM module_events WHERE user_id='old'").fetchone()[0], 0)

    def test_intake_survives_before_maintenance_creates_module_tables(self):
        for table in ('module_events', 'module_time_points', 'module_exit_points'):
            self.db.execute('DROP TABLE ' + table)
        self.populate()
        self.assertGreater(self.db.execute('SELECT COUNT(*) FROM analytics_event_facts').fetchone()[0], 10)

    def test_module_catalog_matches_time_columns(self):
        self.assertEqual(TIME_COLUMNS[:-1], MODULES)
        self.assertEqual(len(set(MODULES)), len(MODULES))


if __name__ == '__main__':
    unittest.main()
