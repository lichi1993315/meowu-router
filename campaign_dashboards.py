"""Campaign coverage from event-ID-deduplicated facts; no inferred conversions."""

DESCRIPTION = ('按事件发生时间筛选，次数按 event_id 去重；已登录玩家和匿名身份分别去重，二者不可相加为真实人数。'
    '前往 Steam 不证明加入愿望单，问卷点击不证明提交。服务端发奖成功与客户端回执分开，步骤覆盖不是严格漏斗。'
    '默认全部批次，避免未入岛的主菜单事件被批次筛选隐藏；旧版本未采集的行为无法补算。')

# Preserve the distinction between client intent, presentation, and server outcomes.
STAGES = [
    ('survey_click', '', '问卷按钮点击'),
    ('wishlist_cta', 'click', '打开领猫入口'),
    ('wishlist_cta', 'steam_open', '加入愿望单按钮 · 前往 Steam'),
    ('wishlist_cta', 'claim_click', '点击领取大佬猫'),
    ('wishlist_reward_claimed', '', '大佬猫发奖成功 · 服务端'),
    ('wishlist_reward_failed', '', '大佬猫领取失败 · 服务端'),
    ('wishlist_cta', 'exposure', '退出邀请曝光'),
    ('wishlist_cta', 'reward_exposure', '领猫页面曝光'),
    ('wishlist_cta', 'reward_close', '关闭领猫页面'),
    ('wishlist_cta', 'exit_close', '关闭退出邀请'),
    ('wishlist_cta', 'dismiss', '关闭邀请 · 旧埋点'),
    ('wishlist_cta', 'claimed', '领取成功回执 · 客户端'),
    ('wishlist_reward_exposure', '', '领猫页面曝光 · 服务端旧埋点'),
    ('wishlist_reward_steam', '', '前往 Steam · 服务端旧埋点'),
]
IDENTIFIED = "user_id NOT LIKE 'anon:%' AND user_id NOT IN ('','unknown','anonymous','anonymous_user')"
COUNTS = f"""COUNT(DISTINCT event_id) 事件次数,
    COUNT(DISTINCT CASE WHEN {IDENTIFIED} THEN user_id END) 已登录玩家数,
    COUNT(DISTINCT CASE WHEN user_id LIKE 'anon:%' THEN user_id END) 匿名身份数,
    COUNT(DISTINCT CASE WHEN user_id IN ('','unknown','anonymous','anonymous_user') THEN event_id END) 身份缺失事件数"""


def extend_dashboards(result, dashboard, panel, scope, time_range):
    """Add campaign coverage and navigation without changing ingestion or schemas."""
    values = ','.join("('%s','%s','%s',%d)" % (*s, i) for i, s in enumerate(STAGES))
    prefix = f"""WITH stages(event_type,action,label,sort) AS (VALUES {values}),
      source AS (SELECT * FROM analytics_events WHERE {scope} AND {time_range('occurred_at')}
        AND (event_type='survey_click' OR event_type='wishlist_cta' OR event_type LIKE 'wishlist_reward_%')
        AND (${{user_id:sqlstring}}='' OR user_id=${{user_id:sqlstring}})),
      e AS (SELECT source.*,COALESCE(NULLIF(json_extract(payload_json,'$.action'),''),
        json_extract(payload_json,'$.view_action'),'') action,
        COALESCE(NULLIF(json_extract(payload_json,'$.detail'),''),'未上报') entry FROM source),
      labeled AS (SELECT e.*,COALESCE(s.label,e.event_type||' / '||e.action) label
        FROM e LEFT JOIN stages s ON s.event_type=e.event_type AND (s.action=e.action OR s.action='')) """
    panels=[]
    for pid, stage in enumerate((0,2,3,4),1):
        kind, action, label=STAGES[stage]
        panels.append(panel(pid,label+' · 已登录玩家',prefix+
            f"SELECT COUNT(DISTINCT CASE WHEN {IDENTIFIED} THEN user_id END) 玩家数 FROM e WHERE event_type='{kind}' AND ('{action}'='' OR action='{action}')",
            'stat',description=DESCRIPTION+' 匿名点击见下方覆盖表。'))
    panels += [
        panel(10,'步骤覆盖 · 次数、已登录玩家、匿名身份',prefix+f"SELECT s.label 行为,{COUNTS} FROM stages s LEFT JOIN e ON e.event_type=s.event_type AND (s.action='' OR e.action=s.action) GROUP BY s.sort,s.label ORDER BY s.sort",description=DESCRIPTION),
        panel(11,'入口来源 · 主菜单、暂停菜单及其他入口',prefix+f"SELECT label 行为,entry 入口,{COUNTS} FROM labeled GROUP BY label,entry ORDER BY 事件次数 DESC",description=DESCRIPTION+' 服务端结果没有入口字段时显示未上报，不推断归因。'),
        panel(12,'每日行为 · 北京时间',prefix+f"SELECT date(occurred_at,'+8 hours') 日期,label 行为,{COUNTS} FROM labeled GROUP BY 1,2 ORDER BY 1 DESC,2",description=DESCRIPTION+' 每日人数不可相加为期间去重人数。'),
        panel(13,'领取失败原因',prefix+f"SELECT COALESCE(NULLIF(json_extract(payload_json,'$.error'),''),'未上报') 原因,{COUNTS} FROM e WHERE event_type='wishlist_reward_failed' GROUP BY 1 ORDER BY 事件次数 DESC",description=DESCRIPTION),
        panel(14,'最近事件 · 最多 200 条',prefix+"SELECT occurred_at 时间,user_id,session_id,label 行为,entry 入口,client_platform 平台,release_version 版本,payload_json 原始证据 FROM labeled ORDER BY julianday(occurred_at) DESC,event_id DESC LIMIT 200",description=DESCRIPTION),
    ]
    board=dashboard('gameplay-campaigns','问卷、愿望单与大佬猫',panels)
    board['templating']['list']=[v for v in board['templating']['list'] if v['name'] not in ('session_id','llm_request_id','page')]
    for v in board['templating']['list']:
        if v['name']=='playtest_id': v['current']={'text':'全部','value':'all'}
    board['panels'][0]['options']['content']=DESCRIPTION
    result['gameplay-campaigns.json']=board
    for b in result.values():
        b.setdefault('links',[]).append({'title':board['title'],'type':'link','url':'/d/gameplay-campaigns','includeVars':True,'keepTime':True})
