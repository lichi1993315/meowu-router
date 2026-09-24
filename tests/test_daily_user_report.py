import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import daily_user_report as report
from import_gameplay_telemetry import ensure_schema
from playtime_store import record_play_session_event


class DailyUserReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'data.db'
        self.db = sqlite3.connect(self.path)
        self.db.execute('CREATE TABLE user_sessions(user_id TEXT PRIMARY KEY,is_developer INTEGER DEFAULT 0)')
        ensure_schema(self.db)
        self.day = dt.date(2026, 9, 23)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def event(self, user, session, timestamp, platform='webgl', event='login', **extra):
        payload = dict(user_id=user, session_id=session, timestamp=timestamp,
                       client_platform=platform, **extra)
        record_play_session_event(self.db, payload=payload, headers={},
                                  event_type=event, received_at=timestamp)
        self.db.commit()

    def test_real_views_midnight_cross_platform_and_production_exclusion(self):
        self.event('returning','old','2026-09-22T15:59:00Z')
        self.event('returning','old','2026-09-22T16:01:00Z', event='heartbeat')
        self.event('returning','win','2026-09-23T01:00:00Z','windows')
        self.event('new','new','2026-09-23T15:59:59Z','unknown')
        self.event('tomorrow','tomorrow','2026-09-23T16:00:00Z')
        self.event('editor','editor','2026-09-23T01:00:00Z','editor')
        self.event('devbuild','devbuild','2026-09-23T01:00:00Z',is_development_build=True)
        self.db.execute("INSERT INTO user_sessions VALUES ('developer',1)")
        self.event('developer','developer','2026-09-23T01:00:00Z')
        data = report.collect(self.db, self.day)
        self.assertEqual((data['dau'],data['new_users'],data['sessions']), (2,1,2))
        self.assertEqual(data['platforms'], {'unknown':1,'webgl':1,'windows':1})
        self.assertEqual((data['d1_cohort'],data['d1_returned']), (1,1))
        self.assertEqual(report.collect(self.db,self.day), data)

    def test_empty_cohort_and_beijing_yesterday(self):
        self.assertIn('无新增样本',report.report_text(report.collect(self.db,self.day)))
        self.assertEqual(report.yesterday(dt.datetime(2026,9,23,17,tzinfo=dt.timezone.utc)), self.day)

    def test_dry_run_never_sends_and_sent_report_is_deduplicated(self):
        out = Path(self.temp.name)/'out'
        with patch.object(report,'send',return_value='message-one') as send:
            report.run(self.day,self.path,out,dry_run=True)
            send.assert_not_called()
            report.run(self.day,self.path,out)
            report.run(self.day,self.path,out)
            send.assert_called_once()
            self.assertEqual(json.loads(next(out.glob('*.sent.json')).read_text())['message_id'],'message-one')

    def test_failure_is_not_marked_sent_and_can_retry(self):
        out = Path(self.temp.name)/'out'
        with patch.object(report,'send',side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError): report.run(self.day,self.path,out)
        self.assertEqual(list(out.glob('*.sent.json')),[])
        with patch.object(report,'send',return_value='retried') as send:
            report.run(self.day,self.path,out)
            send.assert_called_once()

    def test_feishu_business_error_raises_and_retry_uuid_is_stable(self):
        client = MagicMock()
        client.post.return_value.json.side_effect = [
            {'code':0,'tenant_access_token':'test'}, {'code':230001},
            {'code':0,'tenant_access_token':'test'}, {'code':0,'data':{'message_id':'ok'}}]
        with patch.dict('os.environ',FEISHU_BOT_API_KEY='test',FEISHU_BOT_API_SECRET='test',FEISHU_CHAT_ID='oc_test'), patch.object(report.httpx,'Client') as factory:
            factory.return_value.__enter__.return_value = client
            with self.assertRaises(RuntimeError): report.send('report',self.day)
            self.assertEqual(report.send('report',self.day),'ok')
        self.assertEqual(client.post.call_args_list[1].kwargs['json']['uuid'], client.post.call_args_list[3].kwargs['json']['uuid'])


if __name__ == '__main__':
    unittest.main()
