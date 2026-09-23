"""Exercise actual ingest and dashboard SQL, including missing and replayed data."""
import json
from tests.test_telemetry_analytics import AnalyticsTests
from telemetry_events import accept_batch
from generate_analytics_dashboards import build


class TheaterAnalyticsTests(AnalyticsTests):
    def emit(self, kind, view='v1', decision='d1', phase='invitation', user='u', **extra):
        payload = dict(theater_event_id='theater-1', theater_type='solo_daydream', view_id=view,
                       decision_id=decision, phase=phase, title='白日梦', **extra)
        event = dict(event_id=f'e-{self.counter}', event_real_time_iso='2026-09-19T10:00:00Z',
                     sequence=self.counter, event_type=kind, payload=payload,
                     actor={'agent_id':user,'is_player':True})
        self.counter += 1
        packet = dict(user_id=user, session_id='s', client_platform='unknown', events=[event])
        accept_batch(self.path, packet, {})
        accept_batch(self.path, packet, {})
        return packet

    def setUp(self):
        super().setUp()
        self.counter = 1

    def query(self, panel_id, **replacements):
        board = build()['gameplay-theater.json']
        sql = next(p for p in board['panels'] if p['id']==panel_id)['targets'][0]['queryText']
        values = {'${client_platform:sqlstring}':"'unknown'", '${release_version:sqlstring}':"'__all__'",
                  '${test_data}':'auto', '${user_id:sqlstring}':"''", '${session_id:sqlstring}':"''",
                  '${theater_event_id:sqlstring}':"''", '${min_invitations:sqlstring}':"'10'",
                  '${page:sqlstring}':"'0'", '${__from}':'1789257600000', '${__to}':'1789862400000'}
        values.update(replacements)
        for key,value in values.items(): sql=sql.replace(key,value)
        cursor=self.db.execute(sql)
        return [dict(zip([c[0] for c in cursor.description],row)) for row in cursor.fetchall()]

    def test_unobserved_is_not_refusal_and_top5_requires_samples(self):
        self.emit('theater_view')
        self.emit('theater_choice', choice_class='continue')
        # Revisited consent in same viewing attempt is not another invitation.
        self.emit('theater_view', decision='d2')
        self.emit('theater_choice', decision='d2', choice_class='decline')
        self.emit('theater_exit', phase='playback', reason='escape', playback_started=True)
        self.emit('theater_view', view='v2')
        self.emit('theater_choice', view='v2', choice_class='decline')
        self.emit('theater_view', view='v3')
        self.emit('theater_exit', view='v3', reason='escape', playback_started=False)
        row=self.query(3)[0]
        self.assertEqual([row[k] for k in ['邀请展示数','选择继续数','直接拒绝数','未观察到选择数','开演后Esc退出数']], [3,1,1,1,1])
        self.assertEqual(row['继续率'],33.33)
        self.assertEqual(self.query(2),[])
        self.assertEqual(len(self.query(2, **{'${min_invitations:sqlstring}':"'3'"})),1)

    def test_explicit_category_only_full_text_and_filters(self):
        original='自由输入原文\n'+('鱼与猫🐱'*90)
        self.emit('theater_choice', phase='input', vocab_type='other', content=original)
        self.emit('theater_choice', phase='vocab_category', vocab_type='other', content=original, prompt_text='想聊什么？')
        self.emit('theater_choice', view='v2', phase='vocab_category', vocab_type='person', content='小明', prompt_text='想聊什么？')
        row=self.query(5)[0]
        self.assertEqual(row['分类选择次数'],2)
        self.assertEqual(row['其他占比'],50)
        self.assertTrue(any(r['原文']==original for r in self.query(6)))
        self.assertEqual(self.query(6, **{'${user_id:sqlstring}':"'another'"}),[])
        self.assertEqual(self.query(6, **{'${theater_event_id:sqlstring}':"'another'"}),[])

    def test_grafana_empty_textbox_is_empty_sql_tokens(self):
        self.emit('theater_view')
        empty = {key:'' for key in ['${user_id:sqlstring}', '${session_id:sqlstring}', '${theater_event_id:sqlstring}', '${min_invitations:sqlstring}']}
        for panel in range(1,7):
            self.query(panel, **empty)
        self.assertEqual(self.query(1, **empty)[0]['事件数'],1)

    def test_empty_coverage_and_unknown_platform(self):
        self.assertIn('尚无',self.query(1)[0]['采集状态'])
        self.assertEqual(self.query(3),[])
        self.emit('theater_view')
        self.assertEqual(self.query(1)[0]['事件数'],1)
        self.assertEqual(self.query(3)[0]['直接拒绝数'],0)
        self.assertEqual(self.query(3)[0]['未观察到选择数'],1)
