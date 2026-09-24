"""将已发布 Grafana 核心指标同步到飞书；只读分析库，不发送群消息。"""
import argparse
import datetime as dt
from decimal import Decimal
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

import httpx

API = 'https://open.feishu.cn/open-apis'
TZ = ZoneInfo('Asia/Shanghai')
SCOPES = {'汇总（跨平台去重）': ['webgl', 'windows'], 'WebGL': ['webgl'], 'Windows': ['windows']}
FIELDS = [
    {'field_name': n, 'type': t} for n, t in [
        ('指标键', 1), ('指标', 1), ('平台', 1), ('数值', 2), ('单位', 1),
        ('有效玩家数', 2), ('样本说明', 1), ('数据状态', 1), ('更新时间（北京时间）', 1),
        ('发行渠道', 1), ('发布版本', 1), ('测试批次', 1), ('统计口径', 1)]]


def collect(db, dashboard, now):
    """三个固定范围，SQL 按文本复用；整次查询限时 90 秒，全部成功后才写飞书。"""
    deadline = time.monotonic() + 90
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    db.row_factory = sqlite3.Row
    records = []
    try:
        db.execute('BEGIN')
        for label, platforms in SCOPES.items():
            cache = {}
            values = {'client_platform:sqlstring': ','.join("'" + p + "'" for p in platforms),
                      'distribution_channel:sqlstring': "'__all__'", 'release_version:sqlstring': "'__all__'",
                      'playtest_id': 'all', 'test_data': 'auto', 'behavior_period': 'all',
                      '__from': str(int((now - dt.timedelta(days=7)).timestamp() * 1000)),
                      '__to': str(int(now.timestamp() * 1000))}
            for panel in dashboard['panels']:
                if panel.get('type') != 'stat':
                    continue
                sql = panel['targets'][0]['queryText']
                sql = re.sub(r'\$\{([^}]+)\}', lambda m: values[m[1]], sql)
                if sql not in cache:
                    rows = db.execute(sql).fetchall()
                    if len(rows) != 1:
                        raise ValueError('Expected one aggregate row')
                    cache[sql] = dict(rows[0])
                row = cache[sql]
                transforms = panel.get('transformations', [])
                name = transforms[0]['options']['include']['names'][0] if transforms else next(iter(row))
                value = row[name]
                if value is not None and not isinstance(value, (int, float)):
                    raise ValueError('Metric is not numeric')
                pid = panel['id']
                sample = row.get({11: '金币有效玩家数', 12: '猫有效玩家数'}.get(pid, '有效玩家数'))
                unit = ('分钟' if pid in (5, 6) else '游戏日' if pid in (7, 8) else '级' if pid in (9, 10)
                        else '金币' if pid == 11 else '只' if pid == 12 else '次' if pid == 20
                        else 'Token' if pid == 21 else 'USD' if pid == 22 else '%' if pid == 23 else '人')
                period = '近7天开始的会话，渠道人数不可相加。' if pid in (40, 41, 42) else '累计玩家/行为；状态取最新快照；新增按北京时间完整自然日。'
                fields = {'指标键': f'{label}:{pid}', '指标': panel['title'], '平台': label,
                          '数值': value, '单位': unit, '有效玩家数': sample,
                          '样本说明': '对应指标的非空玩家数' if sample is not None else '原指标未提供独立样本数',
                          '数据状态': '无有效样本' if value is None else '已同步',
                          '更新时间（北京时间）': now.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S'),
                          '发行渠道': '全部', '发布版本': '全部', '测试批次': '全部',
                          '统计口径': period + '仅WebGL/Windows，排除开发包和开发者。' + panel.get('description', '')}
                records.append({'fields': fields})
        return records
    finally:
        db.rollback()
        db.set_progress_handler(None, 0)


class Feishu:
    """只记录业务错误码，禁止将凭证或请求正文写入日志。"""
    def __init__(self):
        self.client = httpx.Client(timeout=30)
        self.token = ''
        result = self.call('/auth/v3/tenant_access_token/internal', {
            'app_id': os.environ['FEISHU_BOT_API_KEY'], 'app_secret': os.environ['FEISHU_BOT_API_SECRET']})
        self.token = result['tenant_access_token']

    def call(self, path, body=None, method='POST'):
        response = self.client.request(method, API + path, json=body,
            headers={'Authorization': 'Bearer ' + self.token} if self.token else {})
        result = response.json()
        if result.get('code') != 0:
            raise RuntimeError(f"Feishu code={result.get('code')}")
        response.raise_for_status()
        return result

    def items(self, path):
        """分页上限避免错误配置导致无限遍历。"""
        items, cursor = [], ''
        for _ in range(20):
            result = self.call(path + '?page_size=100' + ('&page_token=' + cursor if cursor else ''), method='GET')['data']
            items.extend(result.get('items') or [])
            if not result.get('has_more'):
                return items
            cursor = result['page_token']
        raise RuntimeError('Pagination limit exceeded')


def save(path, data):
    """创建步骤完成后原子保存资源 ID，重试复用已经创建的表格。"""
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def provision(api, state, path):
    """新建专用表，并授权现有管理员；不发送通知。"""
    if not state.get('app'):
        app = api.call('/bitable/v1/apps', {'name': '游戏运营报表', 'time_zone': 'Asia/Shanghai'})['data']['app']
        state.update(app=app['app_token'], url=app['url'])
        save(path, state)
    app = state['app']
    if not state.get('table'):
        tables = api.items(f'/bitable/v1/apps/{app}/tables')
        table = next((t for t in tables if t['name'] == '核心指标'), None)
        if table is None:
            table = api.call(f'/bitable/v1/apps/{app}/tables', {'table': {
                'name': '核心指标', 'default_view_name': '全部指标', 'fields': FIELDS}})['data']
        state['table'] = table['table_id']
        save(path, state)
    fields = api.items(f"/bitable/v1/apps/{app}/tables/{state['table']}/fields")
    platform_id = next(f['field_id'] for f in fields if f['field_name'] == '平台')
    view_path = f"/bitable/v1/apps/{app}/tables/{state['table']}/views"
    views = api.items(view_path)
    for label in SCOPES:
        view = next((v for v in views if v['view_name'] == label), None)
        if view is None:
            view = api.call(view_path, {'view_name': label, 'view_type': 'grid'})['data']['view']
        api.call(view_path + '/' + view['view_id'], {'property': {'filter_info': {
            'conjunction': 'and', 'conditions': [{'field_id': platform_id, 'operator': 'is', 'value': json.dumps([label])}]}}}, 'PATCH')
    admin = os.getenv('FEISHU_OPERATIONS_MEMBER_ID') or os.environ['FEISHU_ADMIN_ID']
    member_type = os.getenv('FEISHU_ADMIN_MEMBER_TYPE') or ('openid' if admin.startswith('ou_') else 'openchat' if admin.startswith('oc_') else 'email' if '@' in admin else 'userid')
    api.call(f'/drive/v1/permissions/{app}/members?type=bitable&need_notification=false',
             {'member_type': member_type, 'member_id': admin, 'perm': 'edit'})
    return state


def sync(api, state, records):
    """按固定指标键更新；重试先读远端，避免超时重试产生重复记录。"""
    path = f"/bitable/v1/apps/{state['app']}/tables/{state['table']}/records"
    existing = api.items(path)
    ids = {}
    for record in existing:
        key = record['fields'].get('指标键')
        if isinstance(key, list):
            key = ''.join(v.get('text', '') for v in key)
        if key in ids:
            raise RuntimeError('Duplicate metric key')
        ids[key] = record['record_id']
    creates, updates = [], []
    for record in records:
        key = record['fields']['指标键']
        if key in ids:
            updates.append(dict(record, record_id=ids[key]))
        else:
            creates.append(record)
    if creates:
        api.call(path + '/batch_create', {'records': creates})
    if updates:
        api.call(path + '/batch_update', {'records': updates})
    actual = api.items(path)
    by_key = {}
    for r in actual:
        key = r['fields'].get('指标键')
        if isinstance(key, list):
            key = ''.join(v.get('text', '') for v in key)
        by_key[key] = r['fields']
    for expected in records:
        f = expected['fields']
        remote = by_key.get(f['指标键'], {})
        # 飞书数字字段的读取接口会返回十进制字符串。
        expected_value, actual_value = f['数值'], remote.get('数值')
        same = actual_value is None if expected_value is None else (
            actual_value is not None and Decimal(str(actual_value)) == Decimal(str(expected_value)))
        if f['指标键'] not in by_key or not same:
            raise RuntimeError('Read-back value mismatch')
    return len(records)


def run(args):
    output = Path(os.getenv('OPERATIONS_OUTPUT', '/app/output/operations-report'))
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / 'state.json'
    with (output / 'sync.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        api = Feishu()
        try:
            if args.provision:
                provision(api, state, state_path)
            if not state.get('table'):
                raise RuntimeError('Provision report first')
            started = time.monotonic()
            dashboard = json.loads(Path(os.getenv('DASHBOARD_PATH', '/app/gameplay-overview.json')).read_text())
            db_path = Path(os.getenv('DB_PATH', '/app/data/conversations.db')).resolve()
            with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
                records = collect(db, dashboard, dt.datetime.now(TZ))
            query_seconds = time.monotonic() - started
            count = sync(api, state, records)
            state.update(last_success=dt.datetime.now(TZ).isoformat(), records=count, query_seconds=round(query_seconds, 3))
            save(state_path, state)
            print(json.dumps(state, ensure_ascii=False), flush=True)
        finally:
            api.client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provision', action='store_true')
    parser.add_argument('--loop', action='store_true')
    args = parser.parse_args()
    if args.loop and args.provision:
        parser.error('Provision once before starting the scheduled service')
    while True:
        try:
            run(args)
        except Exception as error:
            # 日志不带响应正文和请求 URL，避免泄露凭证或报表内容。
            logging.error('Operations report failed: %s%s', type(error).__name__,
                          (' ' + str(error)) if isinstance(error, RuntimeError) else '')
            if not args.loop:
                raise SystemExit(1)
        if not args.loop:
            break
        time.sleep(600)
