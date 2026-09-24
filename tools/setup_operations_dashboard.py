#!/usr/bin/env python3
"""用官方 Lark CLI 为运营报表创建原生仪表盘；重复执行复用同名组件。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
from urllib.request import Request, urlopen


def plan(overview):
    """使用核心指标表及每日趋势表，所有累计指标固定到跨平台去重汇总。"""
    blocks = [{'name':'阅读说明','type':'text','data_config':{'text':
        '# 游戏运营报表\n每约10分钟同步；统计时间见核心指标表。\n累计指标按玩家去重，状态取最新快照；平台、渠道人数不可相加。\n趋势为最近30个北京时间自然日，今日未结束。AI明细与累计快照覆盖不同，缺失不记为零。'}}]
    for p in overview['panels']:
        if p.get('type') != 'stat':
            continue
        pid = p['id']
        precision = 6 if pid == 22 else 2 if pid in (5,6,7,8,9,10,23) else 0
        blocks.append({'name':p['title'] + ('（%）' if pid == 23 else ''), 'type':'statistics','data_config':{
            'table_name':'核心指标', 'series':[{'field_name':'数值','rollup':'MAX'}],
            'number_format':{'formatName':'dollar_rounded' if pid == 22 else 'digital','precision':precision},
            'filter':{'conjunction':'and','conditions':[{'field_name':'指标键','operator':'is','value':f'汇总（跨平台去重）:{pid}'}]}}})
    for title, metric in [('平台玩家数（跨平台有重叠）','玩家数'),('平台时长中位数（分钟）','时长中位数（分钟）')]:
        blocks.append({'name':title,'type':'column','data_config':{
            'table_name':'核心指标','series':[{'field_name':'数值','rollup':'MAX'}],
            'group_by':[{'field_name':'平台','mode':'integrated'}],
            'filter':{'conjunction':'and','conditions':[{'field_name':'指标','operator':'is','value':metric},
                {'field_name':'平台','operator':'isNot','value':'汇总（跨平台去重）'}]}}})
    blocks.append({'name':'近7天渠道入岛玩家（各渠道不可相加）','type':'bar','data_config':{
        'table_name':'核心指标','series':[{'field_name':'数值','rollup':'MAX'}],
        'group_by':[{'field_name':'指标','mode':'integrated'}],
        'filter':{'conjunction':'and','conditions':[{'field_name':'指标','operator':'contains','value':'期间入岛玩家'},
            {'field_name':'平台','operator':'is','value':'汇总（跨平台去重）'}]}}})
    for metric in ('每日活跃玩家','每日AI Tokens','每日AI成本'):
        blocks.append({'name':metric+' · 最近30天','type':'line','data_config':{
            'table_name':'每日趋势','series':[{'field_name':'数值','rollup':'MAX'}],
            'group_by':[{'field_name':'统计日期','mode':'integrated','sort':{'type':'group','order':'asc'}}],
            'filter':{'conjunction':'and','conditions':[{'field_name':'指标','operator':'is','value':metric},
                {'field_name':'平台','operator':'is','value':'汇总（跨平台去重）'}]}}})
    return blocks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', default='lark-cli')
    parser.add_argument('--env-file', default='.env')
    parser.add_argument('--state', default='output/operations-report/state.json')
    parser.add_argument('--plan-only', action='store_true')
    args = parser.parse_args()
    overview = json.loads(Path('grafana/provisioning/dashboards/gameplay-overview.json').read_text())
    blocks = plan(overview)
    if args.plan_only:
        print(json.dumps(blocks,ensure_ascii=False,indent=2))
        return
    state_path = Path(args.state)
    state = json.loads(state_path.read_text())
    env = dict(os.environ)
    values = dict(line.split('=',1) for line in Path(args.env_file).read_text().splitlines() if '=' in line and not line.startswith('#'))
    env.update(LARKSUITE_CLI_APP_ID=values['FEISHU_BOT_API_KEY'].strip().strip('"'),
               LARKSUITE_CLI_APP_SECRET=values['FEISHU_BOT_API_SECRET'].strip().strip('"'),
               LARKSUITE_CLI_BRAND='feishu',LARKSUITE_CLI_CONFIG_DIR='/tmp/operations-lark-config',
               LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1')

    request = Request('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                      data=json.dumps({'app_id':env['LARKSUITE_CLI_APP_ID'],
                                       'app_secret':env['LARKSUITE_CLI_APP_SECRET']}).encode(),
                      headers={'Content-Type':'application/json'})
    with urlopen(request,timeout=30) as response:
        auth = json.load(response)
    if auth.get('code') != 0:
        raise RuntimeError('Feishu application authentication failed')
    env['LARKSUITE_CLI_TENANT_ACCESS_TOKEN'] = auth['tenant_access_token']

    def cli(command, *flags):
        result = subprocess.run([args.cli,'base',command,'--base-token',state['app'],'--as','bot',*flags],
                                env=env,text=True,capture_output=True)
        if result.returncode:
            # CLI 的结构化诊断含缺失 scope/后台入口，不含凭证。
            print(result.stderr)
            raise SystemExit(result.returncode)
        response = json.loads(result.stdout)
        if response.get('ok') is not True:
            raise RuntimeError('Unexpected CLI response')
        return response['data']

    # 先发现目录，不能因权限错误误判仪表盘不存在。
    dashboards = cli('+dashboard-list')
    print(json.dumps({'discovery':dashboards},ensure_ascii=False))
    # 仪表盘仅在当前任务首次创建；ID单独落盘，避免影响同步任务状态文件。
    dashboard_path = state_path.with_name('dashboard.json')
    saved = json.loads(dashboard_path.read_text()) if dashboard_path.exists() else {}
    if not saved.get('dashboard_id'):
        created = cli('+dashboard-create','--name','运营总览')
        print(json.dumps(created,ensure_ascii=False))
        dashboard = created['dashboard']
        saved['dashboard_id'] = dashboard.get('dashboard_id') or dashboard.get('id')
        if not saved['dashboard_id']:
            raise RuntimeError('No dashboard ID returned')
        dashboard_path.write_text(json.dumps(saved,ensure_ascii=False))
    did = saved['dashboard_id']
    cli('+table-list')
    cli('+field-list','--table-id',state['table'])
    cli('+field-list','--table-id',state['trend_table'])
    existing = cli('+dashboard-block-list','--dashboard-id',did,'--page-size','100')
    # CLI 的分页结构首次现场确认后继续；无法识别时禁止盲目重复创建。
    items = existing.get('items') or existing.get('blocks') or []
    if existing.get('has_more'):
        raise RuntimeError('Unexpected dashboard pagination')
    names = {b['name'] for b in items}
    for block in blocks:
        if block['name'] in names:
            continue
        cli('+dashboard-block-create','--dashboard-id',did,'--name',block['name'],'--type',block['type'],
            '--data-config',json.dumps(block['data_config'],ensure_ascii=False))
        print('created '+block['name'],flush=True)
    cli('+dashboard-arrange','--dashboard-id',did)
    print(json.dumps({'dashboard_id':did,'url':state['url']+'?table='+did,'blocks':len(blocks)},ensure_ascii=False))


if __name__ == '__main__':
    main()
