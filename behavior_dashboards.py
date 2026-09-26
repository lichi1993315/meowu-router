"""Native Grafana behavior views. Cohort filters apply at entry, not to future returns."""

CATEGORY = "CASE category WHEN 'ai' THEN 'AI 对话/内容' WHEN 'cat' THEN '猫亲密互动' WHEN 'fishing' THEN '钓鱼' WHEN 'farming' THEN '种田' WHEN 'restaurant' THEN '餐厅' ELSE '其他' END"
PREFERENCE = "CASE preference WHEN 'ai' THEN 'AI 偏好' WHEN 'simulation' THEN '经营偏好' WHEN 'mixed' THEN '混合' WHEN 'other' THEN '其他偏好' ELSE '证据不足/浅尝' END"
STYLE = "CASE participation_style WHEN 'active' THEN '主动为主' WHEN 'idle' THEN '挂机为主' WHEN 'intermittent' THEN '间歇参与' ELSE '证据不足' END"
PAGE = "LIMIT 200 OFFSET (CASE WHEN ${page:sqlstring} IN ('0','1','2','3','4','5','6','7','8','9') THEN CAST(${page:sqlstring} AS INTEGER) ELSE 0 END)*200"
DESCRIPTION = ('画像窗口固定为首次可操作入岛后 24 小时，规则 portrait-v1。主动时长为 15 秒操作窗口估算；'
               '观看、等待、前台无操作宽限、挂机、后台分别记录。无观察证据不补零。'
               '时间/平台/版本/渠道/批次筛选选择首次入岛 cohort，后续回访允许跨平台；显示实际样本数。'
               '第 2–7 天=[24h,168h)，D7=[168h,192h)。未成熟为空，相关性不代表因果。')


def cohort(scope, time_range):
    return f"""WITH settings AS (SELECT julianday('now') cutoff),
    candidates AS (SELECT p.*,COALESCE(u.is_developer,0) is_developer FROM behavior_players p
      LEFT JOIN user_sessions u USING(user_id)),
    c AS (SELECT * FROM candidates WHERE {scope} AND {time_range('first_at')}
      AND (${{user_id:sqlstring}}='' OR user_id=${{user_id:sqlstring}})
      AND (${{behavior_cohort:sqlstring}}='all' OR cohort_kind='new_observed')) """


def outcomes(prefix):
    # The projection already removed concurrent time. Clip only to outcome windows.
    return prefix + """, eligible_intervals AS (
      SELECT i.* FROM behavior_intervals i JOIN c USING(user_id) JOIN analytics_event_facts f USING(event_id)
      WHERE ('${test_data}'='include' OR ('${test_data}'='auto' AND c.client_platform='editor')
        OR ('${test_data}'='only' AND (f.client_platform='editor' OR f.is_development_build=1 OR c.is_developer=1))
        OR ('${test_data}' IN ('auto','exclude') AND f.client_platform!='editor' AND f.is_development_build=0 AND c.is_developer=0))),
    slices AS (
      SELECT c.user_id, i.quality, i.active, i.start_at, i.end_at,
        MAX(0,MIN(julianday(i.end_at),julianday(c.first_at)+7,(SELECT cutoff FROM settings))
              -MAX(julianday(i.start_at),julianday(c.first_at)+1)) /
          NULLIF(julianday(i.end_at)-julianday(i.start_at),0) fraction,
        MAX(0,MIN(julianday(i.end_at),julianday(c.first_at)+8,(SELECT cutoff FROM settings))
              -MAX(julianday(i.start_at),julianday(c.first_at)+7)) /
          NULLIF(julianday(i.end_at)-julianday(i.start_at),0) d7_fraction,
        CAST(julianday(i.start_at)-julianday(c.first_at) AS INTEGER) relative_day
      FROM c JOIN eligible_intervals i USING(user_id)),
    future AS (SELECT user_id,
      SUM(CASE WHEN quality='observed' THEN active*fraction ELSE 0 END)/60 minutes,
      SUM(CASE WHEN fraction>0 AND quality!='observed' THEN 1 ELSE 0 END) gaps,
      SUM(CASE WHEN d7_fraction>0 AND quality!='observed' THEN 1 ELSE 0 END) d7_gaps,
      MAX(CASE WHEN quality='observed' AND active*d7_fraction>0 THEN 1 ELSE 0 END) d7_return
      FROM slices GROUP BY user_id),
    day_numbers(day) AS (VALUES (1),(2),(3),(4),(5),(6)),
    active_days AS (SELECT c.user_id,COUNT(DISTINCT d.day) active_days FROM c CROSS JOIN day_numbers d
      JOIN eligible_intervals i ON i.user_id=c.user_id AND i.quality='observed' AND i.active>0
      AND julianday(i.start_at)<julianday(c.first_at)+d.day+1
      AND julianday(i.end_at)>julianday(c.first_at)+d.day GROUP BY c.user_id),
    category_visits AS (SELECT a.user_id,a.category,
      MAX(julianday(a.occurred_at)>=julianday(c.first_at) AND julianday(a.occurred_at)<julianday(c.first_at)+1) early,
      MAX(julianday(a.occurred_at)>=julianday(c.first_at)+1 AND julianday(a.occurred_at)<julianday(c.first_at)+7) later
      FROM c JOIN behavior_actions a USING(user_id) JOIN analytics_event_facts f USING(event_id)
      WHERE a.initiated=1 AND a.covered=1 AND ('${test_data}'='include' OR ('${test_data}'='auto' AND c.client_platform='editor')
        OR ('${test_data}'='only' AND (f.client_platform='editor' OR f.is_development_build=1 OR c.is_developer=1))
        OR ('${test_data}' IN ('auto','exclude') AND f.client_platform!='editor' AND f.is_development_build=0 AND c.is_developer=0))
      GROUP BY a.user_id,a.category),
    repeats AS (SELECT user_id,SUM(early AND later) repeated_categories FROM category_visits GROUP BY user_id),
    missing AS (SELECT c.user_id,
      SUM(julianday(q.occurred_at)>=julianday(c.first_at)+1 AND julianday(q.occurred_at)<julianday(c.first_at)+7) future_missing,
      SUM(julianday(q.occurred_at)>=julianday(c.first_at)+7 AND julianday(q.occurred_at)<julianday(c.first_at)+8) d7_missing
      FROM c JOIN behavior_quality_points q USING(user_id) GROUP BY c.user_id),
    scored AS (SELECT c.*,
      CASE WHEN (SELECT cutoff FROM settings)>=julianday(first_at)+7 AND COALESCE(f.gaps,0)+COALESCE(m.future_missing,0)=0
        THEN COALESCE(f.minutes,0) END future_minutes,
      CASE WHEN (SELECT cutoff FROM settings)>=julianday(first_at)+7 AND COALESCE(f.gaps,0)+COALESCE(m.future_missing,0)=0
        THEN COALESCE(d.active_days,0) END future_days,
      CASE WHEN (SELECT cutoff FROM settings)>=julianday(first_at)+8 AND COALESCE(f.d7_gaps,0)+COALESCE(m.d7_missing,0)=0
        THEN COALESCE(f.d7_return,0) END d7_return,
      (SELECT cutoff FROM settings)>=julianday(first_at)+1 frozen,
      COALESCE(r.repeated_categories,0) repeated_categories
      FROM c LEFT JOIN future f USING(user_id) LEFT JOIN repeats r USING(user_id) LEFT JOIN missing m USING(user_id) LEFT JOIN active_days d USING(user_id)) """


def extend_dashboards(result, dashboard, panel, scope, time_range):
    prefix = cohort(scope, time_range)
    only_user = "${user_id:sqlstring}!=''"
    timeline = f"""WITH candidates AS (SELECT a.*,f.payload_json,f.metadata_json,f.client_platform,
      f.release_version,f.is_development_build,COALESCE(u.is_developer,0) is_developer
      FROM behavior_actions a JOIN analytics_event_facts f USING(event_id) LEFT JOIN user_sessions u ON u.user_id=a.user_id),
      e AS (SELECT * FROM candidates WHERE {scope} AND {time_range('occurred_at')} AND {only_user}
        AND user_id=${{user_id:sqlstring}} AND (${{session_id:sqlstring}}='' OR session_id=${{session_id:sqlstring}}))
      SELECT e.user_id,e.session_id,datetime(e.occurred_at,'+8 hours') 北京时间,
      ROUND((julianday(e.occurred_at)-julianday(c.first_at))*86400) 入岛后秒,
      {CATEGORY} 玩法,e.label 行为,e.source 来源,e.outcome 结果,e.object_name 对象,e.operation_id,
      e.payload_json 原始内容,e.metadata_json 原始元数据
      FROM e LEFT JOIN behavior_players c USING(user_id)
      ORDER BY julianday(e.occurred_at),e.sequence,e.event_id {PAGE}"""
    detail_prefix = cohort(scope, lambda column: f"({time_range(column)} OR ${{user_id:sqlstring}}!='')")
    intervals = detail_prefix + f"""SELECT i.user_id,i.session_id,datetime(start_at,'+8 hours') 开始,
      datetime(end_at,'+8 hours') 结束,i.quality 证据,
      CASE WHEN i.quality='observed' THEN i.active END 操作估算秒,
      CASE WHEN i.quality='observed' THEN i.viewing END 观看机会秒,
      CASE WHEN i.quality='observed' THEN i.waiting END 等待秒,
      CASE WHEN i.quality='observed' THEN i.idle END 疑似挂机秒,
      CASE WHEN i.quality='observed' THEN i.background END 后台秒,
      CASE WHEN i.quality='observed' THEN i.unknown END 前台未归类秒
      FROM behavior_intervals i JOIN c USING(user_id) WHERE {only_user}
      AND (${{session_id:sqlstring}}='' OR i.session_id=${{session_id:sqlstring}}) AND {time_range('i.start_at')}
      ORDER BY start_at,event_id {PAGE}"""
    detail = outcomes(detail_prefix) + f"""SELECT user_id,datetime(first_at,'+8 hours') 首次观察入岛,cohort_kind 样本来源,quality 证据,
      CASE WHEN frozen THEN {PREFERENCE} ELSE '观察中' END 早期偏好,
      CASE WHEN frozen THEN {STYLE} ELSE '观察中' END 参与方式,
      ROUND(active/60,2) 早期操作分钟,ROUND(ai/60,2) AI分钟,ROUND(cat/60,2) 猫互动分钟,
      ROUND((fishing+farming+restaurant)/60,2) 经营分钟,ROUND(future_minutes,2) 后续操作分钟,
      future_days 后续活跃天数,d7_return D7主动回访,rule_version
      FROM scored ORDER BY first_at,user_id {PAGE}"""
    opening = prefix + f"""SELECT window_minutes 观察分钟,{CATEGORY} 玩法,COUNT(DISTINCT o.user_id) 参与人数,
      SUM(action_count) 动作次数,(SELECT COUNT(*) FROM c) 全部样本数
      FROM behavior_opening o JOIN c USING(user_id) GROUP BY window_minutes,category ORDER BY window_minutes,category"""
    sequence = prefix + f""", ordered AS (
      SELECT a.*,LAG(category) OVER(PARTITION BY a.user_id ORDER BY julianday(occurred_at),sequence,event_id) previous
      FROM behavior_actions a JOIN c USING(user_id) WHERE julianday(occurred_at)>=julianday(c.first_at)
      AND julianday(occurred_at)<julianday(c.first_at)+30.0/1440 AND initiated=1),
      transitions AS (SELECT *,SUM(CASE WHEN previous=category THEN 0 ELSE 1 END)
        OVER(PARTITION BY user_id ORDER BY julianday(occurred_at),sequence,event_id) segment FROM ordered)
      SELECT user_id,segment 顺序,{CATEGORY} 玩法,datetime(MIN(occurred_at),'+8 hours') 首次行为,COUNT(*) 动作次数
      FROM transitions GROUP BY user_id,segment,category ORDER BY user_id,segment {PAGE}"""
    compare = outcomes(prefix) + f""", ranks AS (
      SELECT user_id,preference,future_minutes,ROW_NUMBER() OVER(PARTITION BY preference ORDER BY future_minutes) rn,
        COUNT(*) OVER(PARTITION BY preference) n FROM scored WHERE frozen AND future_minutes IS NOT NULL),
      quantiles AS (SELECT preference,
        AVG(CASE WHEN rn IN ((n+1)/2,(n+2)/2) THEN future_minutes END) p50,
        MAX(CASE WHEN rn=(3*n+3)/4 THEN future_minutes END) p75 FROM ranks GROUP BY preference),
      groups AS (SELECT preference,COUNT(*) samples,COUNT(d7_return) mature,SUM(d7_return) returns,
        COUNT(future_minutes) time_samples,AVG(future_days) days,
        AVG(CASE WHEN future_minutes IS NOT NULL THEN repeated_categories>0 END) repeats
        FROM scored WHERE frozen GROUP BY preference),
      proportions AS (SELECT *,1.0*returns/NULLIF(mature,0) rate FROM groups)
      SELECT {PREFERENCE} 早期画像,g.samples 玩家数,g.mature D7有效分母,g.returns D7回访人数,
        ROUND(100*g.rate,2) D7主动回访率,
        ROUND(100*(g.rate+1.9208/g.mature-1.96*sqrt(g.rate*(1-g.rate)/g.mature+0.9604/(g.mature*g.mature)))/(1+3.8416/g.mature),2) 置信下界,
        ROUND(100*(g.rate+1.9208/g.mature+1.96*sqrt(g.rate*(1-g.rate)/g.mature+0.9604/(g.mature*g.mature)))/(1+3.8416/g.mature),2) 置信上界,
        g.time_samples 后续时长样本,ROUND(q.p50,2) 后续分钟P50,ROUND(q.p75,2) 后续分钟P75,
        ROUND(g.days,2) 后续活跃天数均值,ROUND(g.repeats*100,2) 重复玩法参与率
      FROM proportions g LEFT JOIN quantiles q USING(preference) ORDER BY g.samples DESC"""
    styles = prefix + f"""SELECT {PREFERENCE} 早期偏好,{STYLE} 参与方式,COUNT(*) 玩家数,
      ROUND(SUM(idle)/60,2) 前台挂机分钟,ROUND(SUM(background)/60,2) 后台分钟,
      ROUND(SUM(waiting)/60,2) 等待分钟,ROUND(SUM(viewing)/60,2) 观看机会分钟
      FROM c WHERE (SELECT cutoff FROM settings)>=julianday(first_at)+1
      GROUP BY preference,participation_style"""
    quality = prefix + """SELECT quality 证据状态,cohort_kind 样本来源,COUNT(*) 玩家数,
      SUM((SELECT cutoff FROM settings)<julianday(first_at)+1) 画像观察中,
      SUM((SELECT cutoff FROM settings)<julianday(first_at)+8) D7待观察,
      ROUND(SUM(unknown)/60,2) 未归类前台分钟 FROM c GROUP BY quality,cohort_kind"""
    status = """SELECT status 投影状态,COUNT(*) 玩家数,MAX(point_count) 最大点数,datetime(MIN(updated_at),'+8 hours') 最早更新时间,
      (SELECT COUNT(*) FROM behavior_dirty) 待处理玩家 FROM behavior_projection_status GROUP BY status"""
    first_actions = prefix + f""", ranked AS (
      SELECT a.*,ROW_NUMBER() OVER(PARTITION BY a.user_id ORDER BY julianday(a.occurred_at),a.sequence,a.event_id) rn
      FROM behavior_actions a JOIN c USING(user_id) WHERE a.initiated=1 AND julianday(a.occurred_at)>=julianday(c.first_at))
      SELECT c.user_id,datetime(c.first_at,'+8 hours') 首次入岛,a.label 首个业务行为,a.source 来源,
        ROUND((julianday(a.occurred_at)-julianday(c.first_at))*86400) 首次行为秒,
        CASE WHEN a.event_id IS NULL THEN '未观察到主动业务行为' ELSE '已观察' END 证据
      FROM c LEFT JOIN ranked a ON a.user_id=c.user_id AND a.rn=1 ORDER BY c.first_at,c.user_id {PAGE}"""
    recent = prefix + f""", clipped AS (
      SELECT i.*,MAX(0,MIN(julianday(end_at),(SELECT cutoff FROM settings))-
        MAX(julianday(start_at),(SELECT cutoff FROM settings)-7))/NULLIF(julianday(end_at)-julianday(start_at),0) fraction
      FROM behavior_intervals i JOIN c USING(user_id)),
      recent AS (SELECT user_id,SUM(quality!='observed') gaps,
        SUM(active*fraction) active,SUM(ai*fraction) ai,SUM(cat*fraction) cat,
        SUM((fishing+farming+restaurant)*fraction) sim FROM clipped WHERE fraction>0 GROUP BY user_id),
      labelled AS (SELECT *,CASE WHEN gaps>0 OR active<300 THEN 'insufficient'
        WHEN ai/active>=0.6 THEN 'ai' WHEN sim/active>=0.6 THEN 'simulation'
        WHEN ai>0 AND sim>0 AND (ai+sim)/active>=0.6 THEN 'mixed' ELSE 'other' END preference
        FROM recent WHERE NOT EXISTS(SELECT 1 FROM behavior_quality_points q WHERE q.user_id=recent.user_id
          AND julianday(q.occurred_at)>=(SELECT cutoff FROM settings)-7))
      SELECT c.user_id,CASE WHEN l.user_id IS NULL THEN '证据不足' ELSE {PREFERENCE.replace('preference','l.preference')} END 最近7天偏好,
        ROUND(l.active/60,2) 操作估算分钟,ROUND(l.ai/60,2) AI分钟,ROUND(l.cat/60,2) 猫互动分钟,
        ROUND(l.sim/60,2) 经营分钟 FROM c LEFT JOIN labelled l USING(user_id) ORDER BY c.user_id {PAGE}"""
    missing_detail = prefix + f"""SELECT q.user_id,datetime(q.occurred_at,'+8 hours') 发生时间,q.reason 缺失原因 FROM behavior_quality_points q JOIN c USING(user_id)
      ORDER BY q.occurred_at DESC {PAGE}"""
    boards = [
        ('gameplay-behavior-timeline', '玩家行为时间线', [(1,'逐条行为日志 · 请先选择玩家',timeline),(2,'参与时段 · 请先选择玩家',intervals),(3,'画像证据与后续投入',detail),(4,'最近 7 天动态偏好 · 不用于同期因果比较',recent)]),
        ('gameplay-behavior-opening', '玩家开局行为', [(1,'前 5/15/30 分钟玩法 · 保留提前离开者',opening),(2,'前 30 分钟行为顺序 · 合并连续同类',sequence),(3,'首个主动业务行为 · 来源未知不宣称自主选择',first_actions)]),
        ('gameplay-behavior-cohorts', '玩家画像与留存', [(1,'早期偏好与后续回访',compare),(2,'偏好 × 参与方式',styles),(3,'玩家明细',detail)]),
        ('gameplay-behavior-quality', '行为数据质量', [(1,'画像证据覆盖',quality),(2,'全局投影队列与上限',status),(3,'缺失证据明细',missing_detail)]),
    ]
    for uid,title,panels in boards:
        board=dashboard(uid,title,[panel(pid,name,sql,description=DESCRIPTION) for pid,name,sql in panels])
        board['templating']['list'].append({'name':'behavior_cohort','label':'样本来源','type':'custom',
          'query':'全部观察玩家 : all,亲历创建角色 : new','current':{'text':'全部观察玩家','value':'all'}})
        if uid in ('gameplay-behavior-cohorts','gameplay-behavior-quality'): board['time']={'from':'now-30d','to':'now'}
        board['panels'][0]['options']['content']=DESCRIPTION+'\n\n详细时间线请填写玩家 ID。旧数据无新版时长时显示证据不足。'
        board['panels'][0]['gridPos']['h']=4
        for p in board['panels'][1:]: p['gridPos']['y']+=1
        if uid.endswith('timeline'):
            next(p for p in board['panels'] if p['id']==1)['description']='按事件发生时间、该事件会话平台/版本/渠道/批次筛选；无新版画像的旧玩家仍可查看原始行为。入岛时间未采集时相对秒为空。'
        for p in board['panels']:
            if 'fieldConfig' in p:
                widths={'北京时间':210,'开始':210,'结束':210,'首次观察入岛':210,'首次行为':210,'首次入岛':210,
                        '最早更新时间':210,'发生时间':210,'user_id':160,'session_id':160,'入岛后秒':110,'玩法':145,
                        '行为':155,'来源':170,'结果':110,'对象':140}
                for field,width in widths.items():
                    p['fieldConfig']['overrides'].append({'matcher':{'id':'byName','options':field},
                        'properties':[{'id':'custom.width','value':width}]})
            for override in p.get('fieldConfig',{}).get('overrides',[]):
                if override.get('matcher',{}).get('options') == 'user_id':
                    for prop in override['properties']:
                        if prop['id']=='links':
                            for link in prop['value']:
                                link['title']='查看玩家行为时间线'
                                link['url']=link['url'].replace('/d/gameplay-player-detail','/d/gameplay-behavior-timeline')
        if uid.endswith('quality'):
            next(p for p in board['panels'] if p['id']==2)['description']='全局运维指标，不受 cohort、平台或时间筛选；history_limit 不参与完整画像。'
        result[uid+'.json']=board
    links=[{'title':title,'type':'link','url':'/d/'+uid,'includeVars':True,'keepTime':True} for uid,title,_ in boards]
    for board in result.values():
        board.setdefault('links',[]).extend(links)
