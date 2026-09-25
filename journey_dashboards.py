"""Native Grafana journey reports. Cohort range selects players, never clips returns."""

DESCRIPTION = ('按首次被观察到的旅程时间选人，后续进度读取到观察截止时间。默认 7 天未回访；观察不足单列。'
               '有效时间=前台扣除 120 秒后的 AFK；短暂停顿计入。P50/P90 只统计时间证据完整的样本。'
               '多人模式只有一人仍属于多人；同玩时长要求至少两位真人。退出位置不代表退出原因。')


def common(scope, time_range):
    return f"""WITH settings AS (
      SELECT CASE WHEN '${{churn_days}}' IN ('1','3','7') THEN CAST('${{churn_days}}' AS INTEGER) ELSE 7 END days,
        julianday('now') cutoff),
    candidates AS (SELECT j.*,COALESCE(u.is_developer,0) is_developer FROM journey_players j
      LEFT JOIN user_sessions u USING(user_id)),
    cohort AS (SELECT * FROM candidates WHERE {scope} AND {time_range('first_at')}
      AND ('${{cohort_kind}}'='all' OR cohort_kind='new' OR (cohort_kind='unclassified' AND '${{cohort_kind}}'='new'))
      AND ('${{mode_group}}'='all' OR mode_group='${{mode_group}}')
      AND ('${{flow_version}}'='all' OR flow_version='${{flow_version}}')
      AND (${{user_id:sqlstring}}='' OR user_id=${{user_id:sqlstring}})),
    activity AS (SELECT c.user_id,MAX(at) last_at FROM cohort c JOIN (
      SELECT user_id,last_at at FROM journey_players
      UNION ALL SELECT user_id,COALESCE(NULLIF(client_sent_at,''),received_at) FROM play_session_events
       WHERE event_type IN ('login','heartbeat') AND COALESCE(app_state,'foreground')='foreground'
    ) a ON a.user_id=c.user_id WHERE julianday(at)<=(SELECT cutoff FROM settings) GROUP BY c.user_id),
    first_archive AS (SELECT user_id,archive_id FROM (
      SELECT user_id,archive_id,ROW_NUMBER() OVER(PARTITION BY user_id ORDER BY MIN(COALESCE(started_at,last_at)),archive_id) rn
      FROM journey_nodes WHERE archive_id!='' GROUP BY user_id,archive_id) WHERE rn=1),
    n AS (SELECT n.* FROM journey_nodes n JOIN cohort c USING(user_id) LEFT JOIN first_archive a USING(user_id)
      WHERE (n.archive_id=a.archive_id OR n.node_id IN ('startup','loading')) AND n.flow_version=c.flow_version AND julianday(COALESCE(n.started_at,n.last_at))<=(SELECT cutoff FROM settings)),
    departures AS (SELECT x.*,a.last_at,
      CASE WHEN (SELECT cutoff FROM settings)-julianday(a.last_at)>=(SELECT days FROM settings) THEN 1 ELSE 0 END churned
      FROM journey_exits x JOIN cohort c USING(user_id) JOIN activity a USING(user_id)
      WHERE julianday(x.occurred_at)<=(SELECT cutoff FROM settings)
        AND (x.reason IN ('journey_leave','journey_quit','journey_background') OR (SELECT cutoff FROM settings)-julianday(x.occurred_at)>180.0/86400)),
    final_exit AS (SELECT * FROM (SELECT d.*,ROW_NUMBER() OVER(PARTITION BY user_id ORDER BY occurred_at DESC,event_id DESC) rn FROM departures d) WHERE rn=1),
    outcomes AS (SELECT n.*,a.last_at player_last_at,
      ((SELECT cutoff FROM settings)-julianday(n.started_at)>=7) matured_completion,
      ((SELECT cutoff FROM settings)-julianday(n.started_at)>=(SELECT days FROM settings)) matured_churn,
      (n.completed_at IS NOT NULL AND julianday(n.completed_at)<=julianday(n.started_at)+7) completed_in_7d,
      CASE WHEN (x.node_id=n.node_id OR n.node_id='level:' || CAST(x.island_level+1 AS TEXT)) AND x.archive_id=n.archive_id AND x.churned=1
        AND (n.completed_at IS NULL OR julianday(n.completed_at)>(SELECT cutoff FROM settings)) THEN 1 ELSE 0 END churned_here
      FROM n JOIN activity a USING(user_id) LEFT JOIN final_exit x USING(user_id)),
    duration_ranks AS (SELECT node_id,effective,foreground,waiting,cumulative_effective,world_effective,
      ROW_NUMBER() OVER(PARTITION BY node_id ORDER BY effective) er,
      ROW_NUMBER() OVER(PARTITION BY node_id ORDER BY cumulative_effective) cr,
      ROW_NUMBER() OVER(PARTITION BY node_id ORDER BY foreground) fr,
      ROW_NUMBER() OVER(PARTITION BY node_id ORDER BY waiting) wr,
      ROW_NUMBER() OVER(PARTITION BY node_id ORDER BY world_effective) ir,
      COUNT(*) OVER(PARTITION BY node_id) samples
      FROM outcomes WHERE quality='observed' AND effective IS NOT NULL AND completed_at IS NOT NULL AND julianday(completed_at)<=(SELECT cutoff FROM settings)),
    quantiles AS (SELECT node_id,COUNT(*) samples,
      MIN(CASE WHEN er>=samples*0.5 THEN effective END)/60 p50,
      MIN(CASE WHEN er>=samples*0.9 THEN effective END)/60 p90,
      MIN(CASE WHEN cr>=samples*0.5 THEN cumulative_effective END)/60 cumulative_p50,
      MIN(CASE WHEN cr>=samples*0.9 THEN cumulative_effective END)/60 cumulative_p90,
      MIN(CASE WHEN fr>=samples*0.5 THEN foreground END)/60 foreground_p50,
      MIN(CASE WHEN wr>=samples*0.5 THEN waiting END)/60 waiting_p50,
      MIN(CASE WHEN ir>=samples*0.5 THEN world_effective END)/60 world_p50,
      MIN(CASE WHEN ir>=samples*0.9 THEN world_effective END)/60 world_p90
      FROM duration_ranks GROUP BY node_id),
    unfinished_ranks AS (SELECT node_id,effective,ROW_NUMBER() OVER(PARTITION BY node_id ORDER BY effective) rn,
      COUNT(*) OVER(PARTITION BY node_id) cnt FROM outcomes WHERE completed_at IS NULL AND quality='observed'),
    unfinished AS (SELECT node_id,MIN(CASE WHEN rn>=cnt*0.5 THEN effective END)/60 p50,
      MIN(CASE WHEN rn>=cnt*0.9 THEN effective END)/60 p90 FROM unfinished_ranks GROUP BY node_id)
    """


def flow_query(prefix):
    return prefix+"""SELECT cat.title 节点,cat.node_id,
      COUNT(DISTINCT o.user_id) 到达人数,
      SUM(CASE WHEN o.completed_at IS NOT NULL THEN 1 ELSE 0 END) 已完成人数,
      SUM(CASE WHEN o.matured_completion THEN 1 ELSE 0 END) 完成率分母,
      SUM(CASE WHEN o.matured_completion AND o.completed_in_7d THEN 1 ELSE 0 END) 七日完成人数,
      ROUND(100.0*SUM(CASE WHEN o.matured_completion AND o.completed_in_7d THEN 1 ELSE 0 END)
        /NULLIF(SUM(CASE WHEN o.matured_completion THEN 1 ELSE 0 END),0),1) 七日完成率,
      ROUND(q.p50,2) 完成本步分钟P50,ROUND(q.p90,2) 完成本步分钟P90,q.samples 完整耗时样本数,
      ROUND(q.cumulative_p50,2) 累计分钟P50,ROUND(q.cumulative_p90,2) 累计分钟P90,
      ROUND(q.foreground_p50,2) 前台分钟P50,ROUND(q.waiting_p50,2) 系统等待分钟P50,
      ROUND(u.p50,2) 未完成停留分钟P50,ROUND(u.p90,2) 未完成停留分钟P90,
      SUM(CASE WHEN o.matured_churn THEN o.churned_here ELSE 0 END) 流失人数,
      SUM(CASE WHEN o.matured_churn THEN 1 ELSE 0 END) 流失率分母,
      ROUND(100.0*SUM(CASE WHEN o.matured_churn THEN o.churned_here ELSE 0 END)
        /NULLIF(SUM(CASE WHEN o.matured_churn THEN 1 ELSE 0 END),0),1) 流失率,
      SUM(CASE WHEN NOT o.matured_churn THEN 1 ELSE 0 END) 观察中,
      SUM(CASE WHEN o.completed_at IS NULL AND EXISTS(SELECT 1 FROM departures d WHERE d.user_id=o.user_id
        AND d.archive_id=o.archive_id AND d.node_id=o.node_id AND julianday(d.occurred_at)<julianday(o.player_last_at)) THEN 1 ELSE 0 END) 已回访未完成,
      SUM(CASE WHEN o.quality!='observed' AND o.status!='skipped' THEN 1 ELSE 0 END) 数据不足,
      SUM(CASE WHEN o.status='skipped' THEN 1 ELSE 0 END) 主动跳过,
      SUM(CASE WHEN o.status IN ('joined_existing','observed_on_return') THEN 1 ELSE 0 END) 加入已有或回访观察,
      (SELECT COUNT(DISTINCT user_id) FROM departures d WHERE d.node_id=cat.node_id) 离开人数,
      (SELECT COUNT(*) FROM departures d WHERE d.node_id=cat.node_id) 离开次数
      FROM journey_catalog cat LEFT JOIN outcomes o ON o.node_id=cat.node_id AND o.flow_version=cat.flow_version
      LEFT JOIN quantiles q ON q.node_id=cat.node_id LEFT JOIN unfinished u ON u.node_id=cat.node_id
      WHERE cat.flow_version='journey-v1' AND cat.node_id NOT LIKE 'level:%'
      GROUP BY cat.node_id ORDER BY cat.sort_order"""


def extend_dashboards(result,dashboard,panel,scope,time_range):
    prefix=common(scope,time_range)
    modes=prefix+"""SELECT c.mode_group 玩家类型,COUNT(*) 玩家数,
      ROUND(SUM(c.single)/60,2) 单人有效分钟,
      ROUND(SUM(c.multiplayer_alone+c.multiplayer_together)/60,2) 多人有效分钟,
      ROUND(SUM(c.multiplayer_alone)/60,2) 多人独自在线分钟,
      ROUND(SUM(c.multiplayer_together)/60,2) 多人同玩分钟,
      ROUND(SUM(c.unknown_mode)/60,2) 模式未采集分钟,
      ROUND(SUM(c.foreground)/60,2) 前台分钟,ROUND(SUM(c.waiting)/60,2) 系统等待分钟,
      SUM(CASE WHEN x.churned=1 THEN 1 ELSE 0 END) 未回访玩家数
      FROM cohort c LEFT JOIN final_exit x USING(user_id) GROUP BY c.mode_group"""
    detail=prefix+"""SELECT o.user_id,o.archive_id,o.title 节点,o.status 状态,o.quality 时间证据,
      o.started_at 开始,o.completed_at 完成,o.claimed_at 领奖,
      ROUND(o.effective/60,2) 有效分钟,ROUND(o.foreground/60,2) 前台分钟,
      ROUND(o.natural_seconds/3600,2) 自然小时,ROUND(o.waiting/60,2) 等待分钟,
      ROUND(o.single/60,2) 单人分钟,ROUND((o.multiplayer_alone+o.multiplayer_together)/60,2) 多人分钟,
      o.progress_value 任务进度,o.target_value 目标,o.player_last_at 最后回访,
      x.node_id 最后节点,x.page_id 最后页面,x.reason 离开证据,x.churned 已流失
      FROM outcomes o LEFT JOIN final_exit x USING(user_id)
      WHERE (${node_id:sqlstring}='' OR o.node_id=${node_id:sqlstring}) ORDER BY o.user_id,o.sort_order LIMIT 500"""
    stages=prefix+"""SELECT cat.node_id,cat.title 节点,COUNT(o.user_id) 已观察人数,
      SUM(o.completed_at IS NOT NULL) 亲历升级人数,SUM(o.status='joined_existing') 加入时已有,
      SUM(o.status='observed_on_return') 回访才观察到,SUM(CASE WHEN o.matured_churn THEN o.churned_here ELSE 0 END) 流失人数,
      SUM(CASE WHEN o.matured_churn THEN 1 ELSE 0 END) 流失率分母,
      ROUND(100.0*SUM(CASE WHEN o.matured_churn THEN o.churned_here ELSE 0 END)/NULLIF(SUM(CASE WHEN o.matured_churn THEN 1 ELSE 0 END),0),1) 流失率,
      ROUND(q.world_p50,2) 入岛至升级分钟P50,ROUND(q.world_p90,2) 入岛至升级分钟P90,
      ROUND(q.p50,2) 本级分钟P50,ROUND(q.p90,2) 本级分钟P90,q.samples 耗时样本数,
      ROUND(SUM(o.single)/60,2) 单人分钟,ROUND(SUM(o.multiplayer_alone)/60,2) 多人独自分钟,
      ROUND(SUM(o.multiplayer_together)/60,2) 多人同玩分钟
      FROM journey_catalog cat LEFT JOIN outcomes o ON o.node_id=cat.node_id AND o.flow_version=cat.flow_version LEFT JOIN quantiles q ON q.node_id=cat.node_id WHERE cat.flow_version='journey-v1' AND cat.node_id LIKE 'level:%' GROUP BY cat.node_id ORDER BY cat.sort_order"""
    bucket="CASE WHEN effective<300 THEN '00 · 0–5 分钟' WHEN effective<600 THEN '01 · 5–10 分钟' WHEN effective<1200 THEN '02 · 10–20 分钟' WHEN effective<1800 THEN '03 · 20–30 分钟' WHEN effective<3600 THEN '04 · 30–60 分钟' WHEN effective<7200 THEN '05 · 60–120 分钟' ELSE '06 · 2 小时以上' END"
    heat=prefix+"""SELECT COALESCE(c.title,x.node_id) 节点,
      SUM(effective<300) "0–5 分钟",SUM(effective>=300 AND effective<600) "5–10 分钟",
      SUM(effective>=600 AND effective<1200) "10–20 分钟",SUM(effective>=1200 AND effective<1800) "20–30 分钟",
      SUM(effective>=1800 AND effective<3600) "30–60 分钟",SUM(effective>=3600 AND effective<7200) "60–120 分钟",
      SUM(effective>=7200) "2 小时以上" FROM final_exit x LEFT JOIN journey_catalog c ON c.node_id=x.node_id AND c.flow_version='journey-v1'
      WHERE churned=1 GROUP BY x.node_id ORDER BY COALESCE(c.sort_order,0)"""
    distribution=prefix+f""", b AS (SELECT *,{bucket} bucket FROM final_exit),
      buckets(label,lo,hi) AS (VALUES ('00 · 0–5 分钟',0,300),('01 · 5–10 分钟',300,600),
      ('02 · 10–20 分钟',600,1200),('03 · 20–30 分钟',1200,1800),('04 · 30–60 分钟',1800,3600),
      ('05 · 60–120 分钟',3600,7200),('06 · 2 小时以上',7200,1e100))
      SELECT label 累计时长,(SELECT COUNT(*) FROM b WHERE bucket=label AND churned=1) 流失人数,
      (SELECT COUNT(*) FROM cohort c WHERE c.effective>=lo) 到达此时长人数,
      ROUND(100.0*(SELECT COUNT(*) FROM b WHERE bucket=label AND churned=1)/NULLIF((SELECT COUNT(*) FROM b WHERE churned=1),0),1) 流失玩家占比
      FROM buckets ORDER BY lo"""
    timeline=prefix+"""SELECT c.user_id,f.occurred_at 时间,f.event_type 事件,
      json_extract(f.payload_json,'$.current_node') 流程位置,json_extract(f.payload_json,'$.current_page') 页面,
      json_extract(f.payload_json,'$.business_type') 服务端结果,json_extract(f.payload_json,'$.business.task_id') task_id,
      json_extract(f.payload_json,'$.mode') 模式,json_extract(f.payload_json,'$.humans') 真人数,
      json_extract(f.payload_json,'$.reason') 原因,json_extract(f.payload_json,'$.result') 结果,
      f.payload_json 原始证据 FROM cohort c JOIN journey_run_owners r USING(user_id)
      JOIN analytics_event_facts f ON f.session_id=r.run_id WHERE f.event_type GLOB 'journey_*'
      AND (${user_id:sqlstring}!='' AND c.user_id=${user_id:sqlstring})
      AND julianday(f.occurred_at)<=(SELECT cutoff FROM settings) ORDER BY f.occurred_at,f.sequence LIMIT 1000"""
    pages=prefix+""", page_totals AS (
      SELECT i.user_id,i.node_id,i.page_id,SUM(i.effective) seconds,SUM(i.waiting) waiting,
        SUM(i.quality!='observed') gaps
      FROM journey_intervals i JOIN cohort c USING(user_id)
      WHERE i.page_id!='' AND i.node_id IN ('player_create','cat_create') GROUP BY i.user_id,i.node_id,i.page_id),
      ranked_pages AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY node_id,page_id ORDER BY seconds) rn,
        COUNT(*) OVER(PARTITION BY node_id,page_id) samples FROM page_totals WHERE gaps=0)
      SELECT CASE node_id WHEN 'player_create' THEN '创建玩家角色' ELSE '创建初始猫' END 流程,
        CASE page_id WHEN 'name' THEN '玩家名字' WHEN 'gender' THEN '玩家性别' WHEN 'nickname' THEN '玩家昵称'
        WHEN 'description' THEN '玩家介绍' WHEN 'outfit' THEN '玩家外观' WHEN 'page_0' THEN '猫咪品种'
        WHEN 'page_1' THEN '猫咪性别' WHEN 'page_2' THEN '听听声音' WHEN 'page_3' THEN '成长经历'
        WHEN 'page_4' THEN '猫咪目标' WHEN 'page_5' THEN '猫咪性格' WHEN 'page_6' THEN '猫咪喜好'
        WHEN 'page_7' THEN '和候选猫聊天' WHEN 'page_9' THEN '猫咪名字' WHEN 'page_10' THEN '确认出发' ELSE '未映射页面' END 页面,
        COUNT(*) 有效样本数,ROUND(MIN(CASE WHEN rn>=samples*0.5 THEN seconds END),2) 页面秒P50,
        ROUND(MIN(CASE WHEN rn>=samples*0.9 THEN seconds END),2) 页面秒P90,
        ROUND(SUM(waiting),2) 总等待秒,
        (SELECT COUNT(*) FROM final_exit x WHERE x.node_id=r.node_id AND x.page_id=r.page_id AND churned=1) 流失人数
      FROM ranked_pages r GROUP BY node_id,page_id ORDER BY node_id,page_id"""
    island_history=prefix+"""SELECT *,ROUND((julianday(服务端升级时间)-julianday(岛屿创建时间))*24,2) 岛屿创建至升级小时 FROM (SELECT c.user_id,f.archive_id,
      json_extract(f.payload_json,'$.business.after_level') 升至等级,
      json_extract(f.payload_json,'$.submitted_by') 提交玩家,
      json_extract(f.payload_json,'$.confirmed_at') 服务端升级时间,
      (SELECT json_extract(g.payload_json,'$.island_created_at') FROM analytics_event_facts g
        WHERE g.archive_id=f.archive_id AND g.event_type='journey_context'
          AND json_extract(g.payload_json,'$.island_created_at') IS NOT NULL ORDER BY g.occurred_at LIMIT 1) 岛屿创建时间
      FROM cohort c JOIN journey_run_owners r USING(user_id) JOIN analytics_event_facts f ON f.session_id=r.run_id
      WHERE f.event_type='journey_business' AND json_extract(f.payload_json,'$.business_type')='island_level_up'
      GROUP BY f.archive_id,json_extract(f.payload_json,'$.confirmation_id') ORDER BY f.occurred_at DESC LIMIT 500)"""
    legacy=f"""WITH source AS (SELECT f.*,COALESCE(u.is_developer,0) is_developer FROM analytics_event_facts f
      LEFT JOIN user_sessions u USING(user_id)), starts AS (
      SELECT user_id,COALESCE(NULLIF(archive_id,''),session_id) archive_key,json_extract(payload_json,'$.task_uid') task_uid,
        json_extract(payload_json,'$.task_id') task_id,json_extract(payload_json,'$.task_title') title,MIN(occurred_at) started_at
      FROM source WHERE {scope} AND event_type='task_started' AND occurred_at IS NOT NULL
        AND json_extract(payload_json,'$.task_uid') IS NOT NULL GROUP BY 1,2,3,4),
      pairs AS (SELECT s.*,MIN(e.occurred_at) completed_at FROM starts s LEFT JOIN analytics_event_facts e
        ON e.user_id=s.user_id AND COALESCE(NULLIF(e.archive_id,''),e.session_id)=s.archive_key
        AND json_extract(e.payload_json,'$.task_uid')=s.task_uid AND json_extract(e.payload_json,'$.task_id')=s.task_id
        AND e.event_type='task_completed' AND julianday(e.occurred_at)>=julianday(s.started_at)
        GROUP BY s.user_id,s.archive_key,s.task_uid,s.task_id)
      SELECT user_id,archive_key,task_id,title 任务,started_at 开始,completed_at 完成,
        ROUND((julianday(completed_at)-julianday(started_at))*24,2) 自然小时,
        '未采集有效时长' 时间证据 FROM pairs WHERE {time_range('started_at')}
        AND (${{user_id:sqlstring}}='' OR user_id=${{user_id:sqlstring}}) ORDER BY started_at DESC LIMIT 500"""
    main="SELECT 节点,node_id,到达人数,七日完成率,完成本步分钟P50,完成本步分钟P90,流失人数,流失率分母,流失率,观察中,数据不足 FROM ("+flow_query(prefix)+")"
    specs=[('gameplay-journey-flow','流程耗时与流失',[
        panel(1,'每个主线节点 · 耗时与流失',main,description=DESCRIPTION),
        panel(2,'节点玩家名单 · 点击 user_id 查看明细',detail,description=DESCRIPTION),
        panel(3,'玩家时间线 · 先选择玩家',timeline,description=DESCRIPTION),
        panel(4,'创建角色与猫 · 页面停留及回看累计',pages,description=DESCRIPTION+' 页面时间包含重复访问，不等同于答题完成耗时。'),
        panel(5,'历史任务 · 仅可证明的自然耗时',legacy,description='按任务开始时间筛选历史记录；严格关联玩家、存档、任务实例和任务 ID。包含离线，不纳入新版有效耗时与流失率。'),
        panel(6,'完整指标 · 完成率分母、累计耗时、未完成停留',flow_query(prefix),description=DESCRIPTION)]),
      ('gameplay-journey-loss','流失时间分布',[
        panel(1,'流失节点 × 累计游玩时间',heat,description=DESCRIPTION),
        panel(2,'流失时间分桶 · 人数与分母',distribution,description=DESCRIPTION),
        panel(3,'每次离开与最后位置',prefix+'SELECT user_id,run_id,archive_id,node_id,page_id,occurred_at,reason,ROUND(effective/60,2) 累计分钟,ROUND(node_effective/60,2) 本步分钟,mode,quality,churned FROM departures ORDER BY occurred_at DESC LIMIT 500',description=DESCRIPTION)]),
      ('gameplay-journey-modes','岛屿成长与单人多人',[
        panel(1,'玩家类型与模式时长',modes,description=DESCRIPTION),
        panel(2,'岛屿 2～8 级 · 玩家经历',stages,description=DESCRIPTION),
        panel(3,'等级与任务逐人明细',detail,description=DESCRIPTION),
        panel(4,'岛屿自身升级记录 · 含真实提交者',island_history,description=DESCRIPTION+' 旧岛没有创建时间时留空；每次升级按确认 ID 去重。')])]
    for uid,title,panels in specs:
        board=dashboard(uid,title,panels)
        board['templating']['list']=[v for v in board['templating']['list'] if v['name'] not in ('session_id','llm_request_id','page')]
        board['templating']['list'] += [
          {'name':'churn_days','label':'未回访天数','type':'custom','query':'1,3,7','current':{'text':'7','value':'7'}},
          {'name':'cohort_kind','label':'玩家范围','type':'custom','query':'首次新玩家与启动期 : new,含存量基线 : all','current':{'text':'首次新玩家与启动期','value':'new'}},
          {'name':'mode_group','label':'玩家模式类型','type':'custom','query':'全部 : all,仅单人 : single,仅多人 : multiplayer,两者都玩 : mixed,未采集 : unknown','current':{'text':'全部','value':'all'}},
          {'name':'flow_version','label':'流程版本','type':'custom','query':'journey-v1','current':{'text':'journey-v1','value':'journey-v1'}},
          {'name':'node_id','label':'节点 ID','type':'textbox','current':{'text':'','value':''}},
        ]
        note=next(p for p in board['panels'] if p['id']==999)
        note['options']['content']=DESCRIPTION+'\n\n时间筛选是首次旅程批次。存量玩家、断档、并发客户端和历史未采集字段单列。七日完成率只纳入已观察满七日的玩家；流失率按所选 1/3/7 日成熟节点为分母。当前节点仅用于定位，不能证明流失原因。'
        drill_filters='&'+ '&'.join('${'+name+':queryparam}' for name in tuple(v['name'] for v in board['templating']['list'] if v['name'] not in ('user_id','node_id','cohort_kind')))
        for p in panels:
            p['fieldConfig']['defaults']['decimals']=2
            p['fieldConfig']['defaults']['mappings']=[{'type':'value','options':{
              'completed':{'text':'已完成'},'in_progress':{'text':'进行中'},'observed':{'text':'完整观测'},
              'missing_start':{'text':'缺少开始时间'},'incomplete':{'text':'证据不完整'},'single':{'text':'仅单人'},
              'multiplayer':{'text':'仅多人'},'mixed':{'text':'两者都玩'},'unknown':{'text':'未采集'},
              'baseline_open':{'text':'已有任务进行中'},'baseline_completed':{'text':'已有任务待领奖'},
              'baseline_claimed':{'text':'已有任务已领奖'},'joined_existing':{'text':'加入时已有'},
              'observed_on_return':{'text':'回访才观察到'},'skipped':{'text':'主动跳过'}}}]
            for override in p['fieldConfig']['overrides']:
                if override['matcher']['options']=='user_id':
                    override['properties'][0]['value'][0]['url']='/d/gameplay-journey-flow?${__url_time_range}&var-user_id=${__value.raw}&var-cohort_kind=all'+drill_filters
            p['fieldConfig']['overrides'].append({'matcher':{'id':'byName','options':'node_id'},'properties':[{'id':'links','value':[{'title':'查看此节点玩家','url':'/d/gameplay-journey-flow?${__url_time_range}&var-node_id=${__value.raw}&var-cohort_kind=${cohort_kind:percentencode}'+drill_filters}]}]})
        result[uid+'.json']=board
    quality=panel(7,'投影健康 · 不受玩家批次筛选',"SELECT status 状态,COUNT(*) 玩家数,MAX(point_count) 最大事件数,MIN(updated_at) 最早刷新 FROM journey_projection_status GROUP BY status UNION ALL SELECT '等待刷新',COUNT(*),NULL,NULL FROM journey_dirty",description='全局数据质量；history_limit 表示超过每玩家十万检查点的保护阈值，已从正式统计撤出，不能当作零耗时。')
    quality['gridPos']={'x':0,'y':100,'w':24,'h':8}
    result['gameplay-journey-flow.json']['panels'].append(quality)
    # A native colored matrix table stays usable with the installed SQLite plugin.
    heatpanel=result['gameplay-journey-loss.json']['panels'][1]
    heatpanel['fieldConfig']['overrides'].append({'matcher':{'id':'byRegexp','options':'分钟|小时以上'},'properties':[
        {'id':'custom.cellOptions','value':{'type':'color-background','mode':'gradient'}},{'id':'color','value':{'mode':'continuous-YlOrRd'}}]})
    links=[{'title':title,'type':'link','url':'/d/'+uid,'includeVars':True,'keepTime':True} for uid,title,_ in specs]
    for board in result.values(): board.setdefault('links',[]).extend(links)
