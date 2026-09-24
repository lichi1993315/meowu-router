import datetime as dt
import sqlite3
import unittest

from operations_report import collect, sync, TZ


class ReportTests(unittest.TestCase):
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
