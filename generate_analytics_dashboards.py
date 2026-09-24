"""Generate Grafana dashboards from shared, reviewable SQL contracts."""
import copy
import json
from pathlib import Path
from theater_analytics import panels as theater_panels

ROOT = Path(__file__).parent / 'grafana/provisioning/dashboards'
DS = {'type': 'frser-sqlite-datasource', 'uid': '${DS_SQLITE}'}
PLATFORM = "('__all__' IN (${client_platform:sqlstring}) OR client_platform IN (${client_platform:sqlstring}))"
VERSION = "('__all__' IN (${release_version:sqlstring}) OR COALESCE(NULLIF(release_version,''),'__empty__') IN (${release_version:sqlstring}))"
TEST = "('${test_data}'='include' OR ('${test_data}'='auto' AND (client_platform='editor' OR (is_development_build=0 AND is_developer=0))) OR ('${test_data}'='exclude' AND is_development_build=0 AND is_developer=0 AND client_platform!='editor') OR ('${test_data}'='only' AND (is_development_build=1 OR is_developer=1 OR client_platform='editor')))"
CHANNEL = "('__all__' IN (${distribution_channel:sqlstring}) OR (user_id,session_id) IN (SELECT user_id,session_id FROM analytics_session_channels WHERE distribution_channel IN (${distribution_channel:sqlstring})) OR ('unknown' IN (${distribution_channel:sqlstring}) AND (user_id,session_id) NOT IN (SELECT user_id,session_id FROM analytics_session_channels)))"
PLAYTEST = "('${playtest_id}'='all' OR ('${playtest_id}'='legacy' AND (user_id,session_id) NOT IN (SELECT user_id,session_id FROM analytics_session_playtests)) OR (user_id,session_id) IN (SELECT user_id,session_id FROM analytics_session_playtests WHERE playtest_id='${playtest_id}'))"
FILTER = f'{PLATFORM} AND {VERSION} AND {TEST} AND {PLAYTEST} AND {CHANNEL}'
COHORT_SCOPE = "CASE WHEN '${test_data}' IN ('include','only') OR ('${test_data}'='auto' AND ('editor' IN (${client_platform:sqlstring}) OR '__all__' IN (${client_platform:sqlstring}))) THEN 'all' ELSE 'production' END"

def time_range(column):
    return f"julianday({column}) >= julianday(${{__from}}/1000,'unixepoch') AND julianday({column}) < julianday(${{__to}}/1000,'unixepoch')"

SESSIONS = f'WITH s AS (SELECT * FROM analytics_sessions WHERE {FILTER} AND {time_range("started_at")} ) '
EVENTS = f'WITH e AS (SELECT * FROM analytics_events WHERE {FILTER} AND {time_range("occurred_at")} ) '
CONVERSATIONS = f"WITH c AS (SELECT c.*, COALESCE(u.is_developer,0) is_developer FROM conversations c LEFT JOIN user_sessions u ON c.user_id=u.user_id), filtered AS (SELECT * FROM c WHERE {FILTER} AND {time_range('timestamp')} AND message_type='chat') "
BOARDS = [('gameplay-overview','经营总览'),('gameplay-retention','新手与留存'),('llm-conversations','猫与 AI 体验'),('gameplay-player-detail','玩家旅程'),('llm-router-dashboard','服务与成本'),('gameplay-quality','数据质量'),('gameplay-theater','玩家小剧场')]


def variables():
    return [
        {'name':'DS_SQLITE','type':'datasource','query':'frser-sqlite-datasource','current':{'text':'SQLite','value':'SQLite'},'hide':2},
        {'name':'client_platform','label':'客户端平台','type':'custom','query':'WebGL : webgl,Windows Player : windows,Unity Editor : editor,其他客户端 : other,未知/历史未上报 : unknown,无客户端归属 : unattributed','multi':True,'includeAll':True,'allValue':"'__all__'",'current':{'text':['WebGL','Windows Player','未知/历史未上报'],'value':['webgl','windows','unknown']}},
        {'name':'distribution_channel','label':'发行渠道','type':'custom','query':'内部版 : internal,Steam : steam,TapTap : taptap,网页版 : web,未知／历史未上报 : unknown','multi':True,'includeAll':True,'allValue':"'__all__'",'current':{'text':'All','value':'$__all'}},
        {'name':'release_version','label':'发布版本','type':'query','datasource':DS,'query':"SELECT DISTINCT COALESCE(NULLIF(release_version,''),'__empty__') AS __text,COALESCE(NULLIF(release_version,''),'__empty__') AS __value FROM analytics_sessions ORDER BY 1 DESC",'refresh':1,'multi':True,'includeAll':True,'allValue':"'__all__'",'current':{'text':'All','value':'$__all'}},
        {'name':'playtest_id','label':'Playtest 批次','type':'custom','query':'全部 : all,中秋playtest : 中秋playtest,历史／未标记 : legacy','current':{'text':'中秋playtest','value':'中秋playtest'}},
        {'name':'test_data','label':'测试数据','type':'custom','query':'按平台自动 : auto,排除 : exclude,包含 : include,仅测试 : only','current':{'text':'按平台自动','value':'auto'}},
        {'name':'user_id','label':'玩家 ID（留空=全部）','type':'textbox','current':{'text':'','value':''}},
        {'name':'session_id','label':'会话 ID（留空=全部）','type':'textbox','current':{'text':'','value':''}},
        {'name':'llm_request_id','label':'AI 请求 ID','type':'textbox','current':{'text':'','value':''}},
        {'name':'page','label':'明细页（每页200条）','type':'custom','query':'0,1,2,3,4,5,6,7,8,9','current':{'text':'0','value':'0'}},
    ]

USER = "(${user_id:sqlstring}='' OR user_id=${user_id:sqlstring})"
SESSION = "(${session_id:sqlstring}='' OR session_id=${session_id:sqlstring})"
REQUEST = "(${llm_request_id:sqlstring}='' OR llm_request_id=${llm_request_id:sqlstring})"
LIMIT = " LIMIT 200 OFFSET (CAST(${page:sqlstring} AS INTEGER)*200)"


def panel(pid,title,query,kind='table',unit='short',description=''):
    p={'id':pid,'title':title,'type':kind,'datasource':DS,'description':description or '遵守时间、平台和版本筛选。缺失值不补零；历史事件没有真实时间时不纳入期间统计。',
       'targets':[{'refId':'A','queryType':'table','queryText':query,'rawQueryText':query}],
       'fieldConfig':{'defaults':{'unit':unit,'custom':{'filterable':True}},'overrides':[]},
       'options':{'showHeader':True,'cellHeight':'sm'} if kind=='table' else {'reduceOptions':{'calcs':['lastNotNull'],'fields':'','values':False},'colorMode':'value','graphMode':'none'}}
    for column,param in [('user_id','user_id'),('session_id','session_id'),('llm_request_id','llm_request_id')]:
        p['fieldConfig']['overrides'].append({'matcher':{'id':'byName','options':column},'properties':[{'id':'links','value':[{'title':'查看玩家旅程','url':f'/d/gameplay-player-detail?${{__url_time_range}}&${{client_platform:queryparam}}&${{release_version:queryparam}}&${{test_data:queryparam}}&${{playtest_id:queryparam}}&${{distribution_channel:queryparam}}&var-user_id=${{__data.fields.user_id}}&var-{param}=${{__value.raw}}'}]}]})
    return p


def dashboard(uid,title,panels):
    links=[{'title':t,'type':'link','url':'/d/'+u,'includeVars':True,'keepTime':True} for u,t in BOARDS]
    links.append({'title':'查看原版报表','type':'link','url':'/d/gameplay-overview-legacy','keepTime':True})
    note={'id':999,'title':'统计口径与历史数据','type':'text','options':{'mode':'markdown','content':'**默认包含 WebGL、Windows Player 和未知/历史未上报；可单独筛选 Unity Editor。** 旧客户端没有上报平台的记录在「未知/历史未上报」中，不能根据版本名推断。平台多选按用户去重。旧游戏事件缺少真实时间，见经营总览及猫与 AI 页的「历史累计」；这些面板不受右上角时间范围影响。期间事件面板为空不代表历史没有行为。所有日期按北京时间。测试数据「按平台自动」会在 Editor 中保留测试记录。'}}
    y=0
    x=0
    for p in [note]+panels:
        if p['type']=='stat':
            p['gridPos']={'x':x,'y':y,'w':12,'h':4}
            x+=12
            if x>=24: x=0; y+=4
        else:
            if x: y+=4; x=0
            h=3 if p['type']=='text' else 9
            p['gridPos']={'x':0,'y':y,'w':24,'h':h}; y+=h
    return {'uid':uid,'title':title,'schemaVersion':39,'version':1,'timezone':'Asia/Shanghai','refresh':'1m','time':{'from':'now-7d','to':'now'},'tags':['PawFishing','analytics-v2'],'templating':{'list':variables()},'links':links,'panels':[note]+panels}


def build():
    result={}
    # Untimed legacy events remain visible without inventing an occurrence date.
    history = f"WITH h AS (SELECT g.*,COALESCE(u.is_developer,0) is_developer FROM gameplay_events g LEFT JOIN user_sessions u ON g.user_id=u.user_id WHERE g.event_real_time_iso IS NULL OR g.event_real_time_iso=''), e AS (SELECT * FROM h WHERE {FILTER}) "
    history_description = '历史缺时间事件的累计值，不受右上角时间范围影响；仍遵守平台、版本和测试数据筛选。不能用于日活或留存。'
    overview=[
      panel(1,'期间入岛玩家数',SESSIONS+'SELECT COUNT(DISTINCT user_id) 玩家数 FROM s','stat',description='按期间开始的去重会话计数；跨午夜活跃请看 DAU 日表。'),
      panel(2,'期间全局新增（首次入岛平台）',SESSIONS+f"SELECT COUNT(DISTINCT s.user_id) 新增 FROM s JOIN analytics_first_entry f ON s.user_id=f.user_id AND s.started_at=f.first_entry_at WHERE f.cohort_scope={COHORT_SCOPE}",'stat'),
      panel(30,'历史累计：缺时间事件与玩家（不受时间筛选）',history+"SELECT COUNT(*) 历史事件数,COUNT(DISTINCT user_id) 玩家数 FROM e",description=history_description),
      panel(31,'历史累计：行为次数与参与人数（不受时间筛选）',history+"SELECT event_type 行为,COUNT(*) 次数,COUNT(DISTINCT user_id) 参与人数 FROM e GROUP BY event_type ORDER BY 次数 DESC LIMIT 100",description=history_description),
      panel(3,'平台对比：去重玩家与会话',SESSIONS+"SELECT client_platform 平台,COUNT(DISTINCT user_id) 玩家数,COUNT(*) 会话数,ROUND(AVG(duration_sec)/60,2) 平均会话分钟,ROUND(SUM(afk_duration_sec)/60,2) 挂机分钟 FROM s GROUP BY client_platform",description='各平台人数不可相加；时长归属于期间开始的会话，不称作每日时长。'),
      panel(4,'会话时长分布',SESSIONS+"SELECT CASE WHEN duration_sec<60 THEN '<1分钟' WHEN duration_sec<300 THEN '1–5分钟' WHEN duration_sec<900 THEN '5–15分钟' WHEN duration_sec<3600 THEN '15–60分钟' ELSE '60分钟以上' END 区间,COUNT(*) 会话数 FROM s GROUP BY 1"),
      panel(5,'核心行为：人数、次数与重复参与',EVENTS+"SELECT event_type 行为,COUNT(*) 次数,COUNT(DISTINCT user_id) 参与人数,ROUND(1.0*COUNT(*)/COUNT(DISTINCT user_id),2) 人均次数 FROM e WHERE actor_is_player=1 GROUP BY event_type ORDER BY 次数 DESC LIMIT 50"),
      panel(6,'期间资源流水',EVENTS+"SELECT json_extract(payload_json,'$.source') 来源,SUM(CASE WHEN json_extract(payload_json,'$.delta')>0 THEN json_extract(payload_json,'$.delta') ELSE 0 END) 获得金币,SUM(CASE WHEN json_extract(payload_json,'$.delta')<0 THEN -json_extract(payload_json,'$.delta') ELSE 0 END) 消耗金币 FROM e WHERE event_type='money_delta' GROUP BY 1"),
      panel(7,'游戏会话状态与时长可信度',SESSIONS+"SELECT client_platform 平台,duration_source 时长来源,confidence 可信度,CASE WHEN status='open' AND julianday(ended_at)<julianday('now','-180 seconds') THEN '心跳过期' ELSE status END 状态,COUNT(*) 会话数 FROM s GROUP BY 1,2,3,4"),
      panel(8,'玩家列表',SESSIONS+"SELECT user_id,COUNT(*) 会话数,GROUP_CONCAT(DISTINCT client_platform) 平台,ROUND(SUM(duration_sec)/60,2) 会话累计分钟,MIN(started_at) 期间首次入岛,MAX(ended_at) 最近活动 FROM s GROUP BY user_id ORDER BY 最近活动 DESC"+LIMIT),
    ]
    for pid,title,event,name in [(20,'购买商品排行','shop_purchase',"COALESCE(json_extract(payload_json,'$.item_name'),json_extract(payload_json,'$.item.item_name'))"),(21,'种植种子排行','farming_plant',"json_extract(payload_json,'$.seed_name')"),(22,'菜谱排行','cooking_completed',"json_extract(payload_json,'$.recipe_name')"),(23,'建筑排行','building_placed',"json_extract(payload_json,'$.building_name')")]:
        overview.append(panel(pid,title,EVENTS+f"SELECT {name} 名称,COUNT(*) 操作次数,COUNT(DISTINCT user_id) 参与人数 FROM e WHERE event_type='{event}' GROUP BY 1 ORDER BY 操作次数 DESC LIMIT 10"))
    result['gameplay-overview.json']=dashboard('gameplay-overview','经营总览',overview)
    # Cohort uses globally determined first entry; return activity is across platforms by default.
    cohort_filter=PLATFORM.replace('OR client_platform IN','OR f.first_platform IN')+' AND '+VERSION.replace("NULLIF(release_version,''),","NULLIF(f.first_release,''),")
    retention=f"""WITH activity AS MATERIALIZED (SELECT DISTINCT user_id,client_platform,is_development_build,activity_day FROM analytics_activity), cohorts AS (SELECT f.* FROM analytics_first_entry f WHERE {cohort_filter} AND f.user_id IN (SELECT user_id FROM analytics_sessions WHERE {FILTER}) AND f.cohort_scope={COHORT_SCOPE} AND ('${{test_data}}'!='only' OR f.first_is_test=1) AND {time_range('f.first_entry_at')}),
    n AS (SELECT 1 d UNION ALL SELECT 3 UNION ALL SELECT 7 UNION ALL SELECT 30)
    SELECT first_day 首次入岛日期,first_platform 首次平台,n.d 留存日,COUNT(*) cohort人数,
    CASE WHEN date(first_day,'+'||n.d||' days')<date('now','+8 hours') THEN SUM(EXISTS(SELECT 1 FROM activity a WHERE (c.cohort_scope='all' OR (a.client_platform!='editor' AND a.is_development_build=0)) AND a.user_id=c.user_id AND a.activity_day=date(c.first_day,'+'||n.d||' days'))) END 任意平台回访人数,
    CASE WHEN date(first_day,'+'||n.d||' days')<date('now','+8 hours') THEN ROUND(100.0*SUM(EXISTS(SELECT 1 FROM activity a WHERE (c.cohort_scope='all' OR (a.client_platform!='editor' AND a.is_development_build=0)) AND a.user_id=c.user_id AND a.activity_day=date(c.first_day,'+'||n.d||' days')))/COUNT(*),2) END 留存率,
    CASE WHEN date(first_day,'+'||n.d||' days')<date('now','+8 hours') THEN ROUND(100.0*SUM(EXISTS(SELECT 1 FROM activity a WHERE (c.cohort_scope='all' OR (a.client_platform!='editor' AND a.is_development_build=0)) AND a.user_id=c.user_id AND a.client_platform=c.first_platform AND a.activity_day=date(c.first_day,'+'||n.d||' days')))/COUNT(*),2) END 原平台留存率
    FROM cohorts c CROSS JOIN n GROUP BY first_day,first_platform,n.d ORDER BY first_day DESC,n.d"""
    activity_filter=FILTER.replace('is_developer','COALESCE(u.is_developer,0)').replace('(user_id,session_id)', '(a.user_id,a.session_id)')
    daily=f"SELECT activity_day 日期,COUNT(DISTINCT a.user_id) DAU FROM analytics_activity a LEFT JOIN user_sessions u ON a.user_id=u.user_id WHERE {activity_filter} AND activity_day>=date(${{__from}}/1000,'unixepoch','+8 hours') AND activity_day<=date(${{__to}}/1000,'unixepoch','+8 hours') GROUP BY activity_day ORDER BY activity_day"
    result['gameplay-retention.json']=dashboard('gameplay-retention','新手与留存',[
      panel(1,'每日游戏活跃（登录/前台心跳）',daily),panel(2,'成熟 cohort 留存；空值=待观察',retention,description='默认正式玩家全局首登；显式选择 Editor 或包含测试数据时使用包含测试的全局首登。任意平台和原平台回访并列，未成熟为空。'),
      panel(3,'新手关键步骤覆盖（非严格顺序漏斗）',EVENTS+"SELECT event_type 步骤,COUNT(DISTINCT user_id) 人数,COUNT(*) 次数 FROM e WHERE event_type IN ('intro_started','profile_form_shown','profile_submitted','intro_completed','intro_skipped','task_started','task_completed','task_claimed','fishing_catch','cat_conversation') GROUP BY event_type",description='历史记录缺流程 ID 时只展示覆盖人数，不伪造严格漏斗转化率。'),
      panel(4,'期间最后行为',EVENTS+"SELECT user_id,session_id,event_type 最后行为,occurred_at 时间 FROM (SELECT *,ROW_NUMBER() OVER(PARTITION BY user_id ORDER BY julianday(occurred_at) DESC,sequence DESC) n FROM e) WHERE n=1 ORDER BY occurred_at DESC"+LIMIT)])
    ai=[
      panel(1,'猫与 AI 事件覆盖',EVENTS+"SELECT event_type,COUNT(*) 次数,COUNT(DISTINCT user_id) 用户数 FROM e WHERE event_type LIKE 'cat_%' OR event_type LIKE 'ai_%' OR event_type='player_cat_interaction' GROUP BY event_type ORDER BY 次数 DESC"),
      panel(2,'工具结果：成功、失败、取消分开',EVENTS+"SELECT json_extract(payload_json,'$.tool') 工具,event_type 结果,json_extract(payload_json,'$.reason') 原因,COUNT(*) 次数 FROM e WHERE event_type IN ('cat_tool_completed','cat_tool_failed','cat_tool_cancelled') GROUP BY 1,2,3 ORDER BY 次数 DESC"),
      panel(3,'AI 使用与费用（游戏结果记录）',EVENTS+"SELECT json_extract(payload_json,'$.model') 模型,json_extract(payload_json,'$.mode') 用途,COUNT(*) 响应数,SUM(json_extract(payload_json,'$.input_tokens')) 输入Token,SUM(json_extract(payload_json,'$.cached_tokens')) 缓存Token,SUM(json_extract(payload_json,'$.output_tokens')) 输出Token,SUM(json_extract(payload_json,'$.estimated_usd')) 估算USD,AVG(json_extract(payload_json,'$.full_latency_ms')) 平均完整响应毫秒 FROM e WHERE event_type='cat_agent_response' GROUP BY 1,2"),
      panel(4,'中转对话与请求关联',CONVERSATIONS+f"SELECT timestamp,user_id,session_id,client_platform,llm_request_id,attempt_id,user_query,ai_response,duration_ms FROM filtered WHERE {USER} AND {SESSION} AND {REQUEST} ORDER BY timestamp DESC"+LIMIT),
      panel(5,'关系与亲密度变化',EVENTS+"SELECT user_id,session_id,occurred_at,event_type,actor_id,payload_json FROM e WHERE event_type IN ('cat_relationship_changed','cat_affection_changed','cat_adopted') ORDER BY occurred_at DESC"+LIMIT),
    ]
    ai.append(panel(30,'历史累计：猫与 AI 行为（不受时间筛选）',history+"SELECT event_type 行为,COUNT(*) 次数,COUNT(DISTINCT user_id) 参与人数 FROM e WHERE event_type LIKE 'cat_%' OR event_type LIKE 'ai_%' OR event_type='player_cat_interaction' GROUP BY event_type ORDER BY 次数 DESC",description=history_description))
    result['conversations.json']=dashboard('llm-conversations','猫与 AI 体验',ai)
    require_user="${user_id:sqlstring}<>'' AND "+USER
    journey=[
      panel(1,'选择玩家后查看会话',SESSIONS+f"SELECT user_id,session_id,player_session_id,client_platform,client_version,started_at,ended_at,ROUND(duration_sec/60,2) 会话分钟,confidence,status FROM s WHERE {require_user} AND {SESSION} ORDER BY started_at DESC"+LIMIT),
      panel(2,'真实事件时间线',EVENTS+f"SELECT user_id,session_id,occurred_at,event_type,actor_id,json_extract(payload_json,'$.request_stats.llm_request_id') llm_request_id,payload_json FROM e WHERE {require_user} AND {SESSION} ORDER BY julianday(occurred_at) DESC,sequence DESC"+LIMIT),
      panel(3,'中转模型记录',CONVERSATIONS+f"SELECT user_id,session_id,timestamp,client_platform,llm_request_id,attempt_id,user_query,ai_response,duration_ms,prompt_tokens,completion_tokens FROM filtered WHERE {require_user} AND {SESSION} AND {REQUEST} ORDER BY timestamp DESC"+LIMIT),
      panel(4,'历史缺时间事件（不受时间筛选，最多200条）',f"SELECT user_id,session_id,game_day,event_index,event_type,payload_json FROM gameplay_events WHERE event_real_time_iso IS NULL AND {PLATFORM} AND {VERSION} AND {require_user} AND {SESSION} ORDER BY game_day DESC,event_index DESC"+LIMIT,description='只有明确选择玩家才能查；这些事件无法还原真实时间，不混入期间时间线。')]
    result['gameplay-player-detail.json']=dashboard('gameplay-player-detail','玩家旅程',journey)
    # Retain the existing service monitoring PromQL panels; remove misleading SQL business trends.
    old=json.loads((ROOT/'llm-router.json').read_text())
    prom=[copy.deepcopy(p) for p in old['panels'] if p.get('targets') and any('expr' in t for t in p['targets'])]
    for p in prom:
        p['title']='服务整体 · '+p['title'].removeprefix('服务整体 · ');p['description']='Prometheus 服务整体指标，不受客户端平台/版本筛选。'
    result['llm-router.json']=dashboard('llm-router-dashboard','服务与成本',prom+copy.deepcopy(ai[2:3]))
    for i,p in enumerate(result['llm-router.json']['panels']):p['id']=1000+i
    result['llm-router.json']['templating']['list'].insert(0,{'name':'DS_PROMETHEUS','type':'datasource','query':'prometheus','current':{'text':'Prometheus','value':'Prometheus'},'hide':2})
    quality=[
      panel(1,'历史全量平台覆盖（不受筛选）',"SELECT client_platform 平台,COUNT(*) 会话数,COUNT(DISTINCT user_id) 玩家数 FROM analytics_sessions GROUP BY client_platform",description='专门展示历史平台缺失，防止默认正式平台筛选掩盖旧数据。'),
      panel(2,'期间事件字段覆盖',EVENTS+"SELECT COUNT(*) 事件数,SUM(event_id IS NOT NULL) 有事件ID,SUM(client_platform NOT IN ('unknown','unattributed')) 已知平台,SUM(json_extract(payload_json,'$.request_stats.llm_request_id') IS NOT NULL) 有模型关联ID FROM e"),
      panel(3,'历史缺时间记录（不受时间筛选）',f"SELECT client_platform,event_type,COUNT(*) 缺时间条数 FROM gameplay_events WHERE event_real_time_iso IS NULL AND {PLATFORM} AND {VERSION} GROUP BY 1,2 ORDER BY 3 DESC"),
      panel(4,'数据更新时间（不受筛选）',"SELECT '心跳' 数据源,MAX(received_at) 最近入库 FROM play_session_events UNION ALL SELECT '增量事件',MAX(received_at) FROM gameplay_live_events UNION ALL SELECT '退出快照',MAX(imported_at) FROM gameplay_sessions UNION ALL SELECT '中转请求',MAX(timestamp) FROM conversations"),
      panel(5,'期间会话可信度',SESSIONS+"SELECT client_platform,duration_source,confidence,COUNT(*) 会话数 FROM s GROUP BY 1,2,3"),
      panel(6,'AI 请求关联覆盖',EVENTS+"SELECT COUNT(*) AI响应数,SUM(EXISTS(SELECT 1 FROM conversations c WHERE c.llm_request_id=json_extract(e.payload_json,'$.request_stats.llm_request_id') AND c.user_id=e.user_id)) 可查中转原文数 FROM e WHERE event_type='cat_agent_response'")]
    result['gameplay-quality.json']=dashboard('gameplay-quality','数据质量',quality)
    for board in result.values():
        if board['uid'] not in ('gameplay-player-detail','llm-conversations'):
            for variable in board['templating']['list']:
                if variable['name'] in ('user_id','session_id','llm_request_id','page'): variable['hide']=2
    theater = dashboard('gameplay-theater', '玩家小剧场', theater_panels(panel, EVENTS.rstrip(), LIMIT))
    theater['templating']['list'] = [v for v in theater['templating']['list'] if v['name'] != 'llm_request_id']
    theater['templating']['list'].extend([
        {'name':'theater_event_id','label':'小剧场事件 ID','type':'textbox','current':{'text':'','value':''}},
        {'name':'min_invitations','label':'Top 5 最少邀请数','type':'textbox','current':{'text':'10','value':'10'}},
    ])
    result['gameplay-theater.json'] = theater
    from player_analytics import extend_dashboards
    extend_dashboards(result, dashboard, panel, FILTER, USER, LIMIT, time_range)
    from player_analytics_legacy import restore_legacy_content
    restore_legacy_content(result, panel, FILTER, time_range)
    # Channel counts describe logins in the selected period, using explicit session markers.
    channel_sessions = SESSIONS.rstrip() + ", channel_sessions AS (SELECT s.user_id,s.session_id,COALESCE(c.distribution_channel,'unknown') channel FROM s LEFT JOIN analytics_session_channels c USING(user_id,session_id)) "
    channel_description = '按所选时间内开始的会话统计，遵守批次、平台、版本、发行渠道和测试数据筛选；各渠道内按玩家去重，同一玩家跨渠道可分别计数，人数不可直接相加。仅使用明确上报的渠道，不按版本名推断；0 表示当前筛选下没有已识别记录。'
    channel_panels = []
    for pid, channel, title in [(40,'steam','Steam · 期间入岛玩家'),(41,'taptap','TapTap · 期间入岛玩家'),(42,'unknown','渠道未上报 · 期间入岛玩家')]:
        channel_panels.append(panel(pid,title,channel_sessions+f"SELECT COUNT(DISTINCT user_id) 玩家数 FROM channel_sessions WHERE channel='{channel}'",'stat',description=channel_description))
    channel_panels.append(panel(43,'发行渠道明细 · 期间入岛玩家与会话',channel_sessions+"""
        , channels(channel,label,sort) AS (VALUES ('steam','Steam',1),('taptap','TapTap',2),
          ('web','网页版',3),('internal','内部版',4),('unknown','未知／历史未上报',5))
        SELECT c.label 发行渠道,COUNT(DISTINCT s.user_id) 玩家数,COUNT(s.session_id) 会话数
        FROM channels c LEFT JOIN channel_sessions s ON s.channel=c.channel
        GROUP BY c.channel,c.label,c.sort ORDER BY c.sort
        """,description=channel_description))
    result['gameplay-overview.json']['panels'].extend(channel_panels)
    from player_analytics_layout import apply_player_layout
    apply_player_layout(result)
    return result

if __name__=='__main__':
    for name,value in build().items():
        (ROOT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
