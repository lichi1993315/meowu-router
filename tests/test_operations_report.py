import datetime as dt
import sqlite3
import unittest

from operations_report import collect, collect_trends, sync, Feishu, TZ


class ReportTests(unittest.TestCase):
    def test_new_empty_feishu_table_returns_null_items(self):
        api = object.__new__(Feishu)
        api.call = lambda *args, **kwargs: {"data": {"items": None, "has_more": False}}
        self.assertEqual(api.items("/records"), [])

    def test_platform_dedup_null_and_zero(self):
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE visits(user_id TEXT, platform TEXT)')
        db.executemany('INSERT INTO visits VALUES (?,?)', [('same','webgl'),('same','windows'),('second','webgl')])
        db.commit()
        dashboard = {'panels': [
            {'id':1, 'type':'stat', 'title':'玩家数', 'targets':[{'queryText': 'SELECT COUNT(DISTINCT user_id) value FROM visits WHERE platform IN (${client_platform:sqlstring})'}]},
            {'id':11, 'type':'stat', 'title':'金币', 'targets':[{'queryText': 'SELECT NULL value, 0 金币有效玩家数'}]},
            {'id':20, 'type':'stat', 'title':'AI调用', 'targets':[{'queryText': 'SELECT 0 value'}]},
        ]}
        records = collect(db, dashboard, dt.datetime(2026, 9, 25, tzinfo=TZ))
        self.assertEqual([r['fields']['数值'] for r in records[::3]], [2, 2, 1])
        self.assertIsNone(records[1]['fields']['数值'])
        self.assertEqual(records[1]['fields']['有效玩家数'], 0)
        self.assertEqual(records[1]['fields']['数据状态'], '无有效样本')
        self.assertEqual(records[2]['fields']['数值'], 0)
        self.assertEqual(records[2]['fields']['数据状态'], '已同步')

    def test_trend_window_dates_and_missing_ai_remain_null(self):
        db = sqlite3.connect(':memory:')
        dau = {'panels':[{'id':1,'targets':[{'queryText':"SELECT '2026-09-25' 日期, 2 DAU"}]}]}
        ai = {'panels':[{'id':27,'targets':[{'queryText':"SELECT '2026-09-25' 日期, 15 TotalTokens, NULL CostUSD"}]}]}
        records = collect_trends(db, ai, dau, dt.datetime(2026,9,25,12,tzinfo=TZ))
        self.assertEqual(len(records),270)
        self.assertEqual(len({r['fields']['指标键'] for r in records}),270)
        self.assertEqual(records[0]['fields']['数值'],2)
        self.assertEqual(records[1]['fields']['数值'],15)
        self.assertIsNone(records[2]['fields']['数值'])
        self.assertEqual(records[3]['fields']['数值'],0)
        self.assertIsNone(records[4]['fields']['数值'])
        self.assertEqual(records[0]['fields']['统计日期'] - records[3]['fields']['统计日期'],86400000)

    def test_unknown_variable_fails_closed(self):
        db = sqlite3.connect(':memory:')
        p = {'type':'stat','targets':[{'queryText':'SELECT ${new_unknown_filter}'}]}
        with self.assertRaises(KeyError):
            collect(db, {'panels':[p]}, dt.datetime.now(TZ))

    def test_query_failure_rolls_back(self):
        db = sqlite3.connect(':memory:')
        p = {'type':'stat','targets':[{'queryText':'SELECT * FROM absent_table'}]}
        with self.assertRaises(sqlite3.OperationalError):
            collect(db, {'panels':[p]}, dt.datetime.now(TZ))
        self.assertFalse(db.in_transaction)

    def test_retry_reads_remote_and_updates_same_record(self):
        api = FakeApi()
        data = [{'fields':{'指标键':'test:1','数值':3}}]
        state = {'app':'app','table':'table'}
        self.assertEqual(sync(api, state, data), 1)
        data[0]['fields']['数值'] = None
        self.assertEqual(sync(api, state, data), 1)
        self.assertEqual(len(api.records), 1)
        self.assertIsNone(api.records[0]['fields']['数值'])
        self.assertEqual(api.calls, ['create','update'])

    def test_duplicate_existing_key_stops_sync(self):
        api = FakeApi()
        api.records = [{'record_id':str(i), 'fields':{'指标键':'dup','数值':1}} for i in range(2)]
        with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
            sync(api, {'app':'app','table':'table'}, [])
        self.assertEqual(api.calls, [])

    def test_feishu_number_string_readback(self):
        api = FakeApi()
        api.records = [{"record_id":"1", "fields":{"指标键":"a", "数值":"0.123456"}}]
        api.call = lambda *args: None
        self.assertEqual(sync(api, {"app":"app","table":"table"}, [{"fields":{"指标键":"a","数值":0.123456}}]), 1)

    def test_readback_mismatch_is_failure(self):
        api = FakeApi()
        api.call = lambda *args: None
        with self.assertRaisesRegex(RuntimeError, 'mismatch'):
            sync(api, {'app':'app','table':'table'}, [{'fields':{'指标键':'a','数值':5}}])


class FakeApi:
    def __init__(self):
        self.records = []
        self.calls = []

    def items(self, path):
        return self.records

    def call(self, path, body):
        import copy
        if path.endswith('batch_create'):
            self.calls.append('create')
            self.records.extend(dict(copy.deepcopy(r), record_id=str(i)) for i,r in enumerate(body['records']))
        else:
            self.calls.append('update')
            for row in body['records']:
                next(r for r in self.records if r['record_id'] == row['record_id'])['fields'] = copy.deepcopy(row['fields'])


if __name__ == '__main__':
    unittest.main()
