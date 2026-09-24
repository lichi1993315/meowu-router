"""AI feature reports: count stable business identities, never infer missing outcomes."""


def panels(q):
    # The caller supplies player/platform/playtest filtered events. Untimed history is
    # retained only in lifetime mode. Scene identities are island scoped, not viewer scoped.
    prefix = """, business AS (SELECT *,
      COALESCE(NULLIF(json_extract(payload_json,'$.flow_id'),''),NULLIF(json_extract(payload_json,'$.theater_event_id'),'')) flow,
      json_extract(payload_json,'$.theater_type') template,
      json_extract(payload_json,'$.phase') phase,
      json_extract(payload_json,'$.participant_count') actors,
      json_extract(payload_json,'$.result') result,
      json_extract(payload_json,'$.reason') reason,
      json_extract(payload_json,'$.entry_id') entry,
      COALESCE(NULLIF(archive_id,''),session_id) island
      FROM events WHERE event_type IN ('theater_lifecycle','theater_line','theater_view','theater_choice','theater_exit',
        'island_lexicon_result','island_lexicon_used','ai_building_generation','ai_building_created','ai_adventure_state','ai_adventure_action',
        'ai_business_request','ai_generated_building_wish','ai_generation_completed','ai_generation_failed','ai_generated_furniture_feedback','building_place')),
    flow_keys AS MATERIALIZED (SELECT DISTINCT island,flow FROM business WHERE flow IS NOT NULL),
    business_history AS (SELECT f.*,
      COALESCE(NULLIF(json_extract(f.payload_json,'$.flow_id'),''),NULLIF(json_extract(f.payload_json,'$.theater_event_id'),'')) flow,
      json_extract(f.payload_json,'$.theater_type') template,
      json_extract(f.payload_json,'$.phase') phase,
      json_extract(f.payload_json,'$.participant_count') actors,
      json_extract(f.payload_json,'$.result') result,
      json_extract(f.payload_json,'$.reason') reason,
      json_extract(f.payload_json,'$.entry_id') entry,
      COALESCE(NULLIF(f.archive_id,''),f.session_id) island

      FROM flow_keys k JOIN analytics_events f ON COALESCE(NULLIF(f.archive_id,''),f.session_id)=k.island AND COALESCE(NULLIF(json_extract(f.payload_json,'$.flow_id'),''),NULLIF(json_extract(f.payload_json,'$.theater_event_id'),''))=k.flow
      WHERE ('${behavior_period}'='all' OR julianday(f.occurred_at)<julianday(${__to}/1000,'unixepoch'))),
    flows_raw AS (SELECT island,flow,MAX(template) template,
      MAX(event_type IN ('theater_lifecycle','ai_adventure_state')) lifecycle,
      MAX(CASE WHEN event_type='ai_adventure_state' THEN 'adventure' ELSE json_extract(payload_json,'$.business_group') END) business_group,
      MIN(CASE WHEN event_type='theater_lifecycle' AND phase='offered' OR event_type='ai_adventure_state' AND json_extract(payload_json,'$.previous_phase')='' THEN occurred_at END) offered,
      MIN(CASE WHEN event_type='theater_line' OR event_type='ai_adventure_state' AND phase IN ('playing_act_1','playing_act_2') THEN occurred_at END) started,
      MIN(CASE WHEN event_type IN ('theater_lifecycle','ai_adventure_state') AND phase='completed' THEN occurred_at END) completed,
      MAX(CASE WHEN event_type='theater_line' OR event_type='ai_adventure_state' AND phase IN ('playing_act_1','playing_act_2') THEN actors END) actual_actors,
      MAX(CASE WHEN phase IN ('offered','inviting','gathering') THEN actors END) planned_actors,
      COUNT(DISTINCT CASE WHEN event_type='theater_view' THEN user_id END) viewers,
      MAX(CASE WHEN event_type IN ('theater_lifecycle','ai_adventure_state') AND phase IN ('cancelled','expired','abandoned','declined') THEN phase END) terminal,
      COUNT(DISTINCT CASE WHEN event_type='theater_line' THEN json_extract(payload_json,'$.scene_id') END) scenes
      FROM business_history WHERE flow IS NOT NULL AND event_type IN ('theater_lifecycle','theater_line','theater_view','theater_choice','theater_exit','ai_adventure_state') GROUP BY island,flow),
    flows AS (SELECT * FROM flows_raw f WHERE (offered IS NULL OR EXISTS(SELECT 1 FROM business origin
      WHERE origin.island=f.island AND origin.flow=f.flow AND origin.occurred_at=f.offered
      AND (origin.event_type='theater_lifecycle' AND origin.phase='offered' OR origin.event_type='ai_adventure_state' AND json_extract(origin.payload_json,'$.previous_phase')='')))
      AND ('${behavior_period}'='all' OR offered IS NULL
      OR (julianday(offered)>=julianday(${__from}/1000,'unixepoch') AND julianday(offered)<julianday(${__to}/1000,'unixepoch')))),
    generation_raw AS (SELECT island,user_id,flow,MIN(CASE WHEN phase='requested' THEN occurred_at END) requested,
      MAX(CASE WHEN phase='completed' THEN 1 ELSE 0 END) succeeded,MAX(CASE WHEN phase='failed' THEN 1 ELSE 0 END) failed,
      MAX(CASE WHEN phase='cancelled' THEN 1 ELSE 0 END) cancelled,
      MAX(json_extract(payload_json,'$.elapsed_seconds')) seconds,MAX(json_extract(payload_json,'$.source_id')) source_id
      FROM business_history WHERE event_type='ai_building_generation' AND flow IS NOT NULL GROUP BY island,user_id,flow),
    generation AS (SELECT * FROM generation_raw g WHERE (requested IS NULL OR EXISTS(SELECT 1 FROM business origin
      WHERE origin.island=g.island AND origin.flow=g.flow AND origin.occurred_at=g.requested AND origin.event_type='ai_building_generation' AND origin.phase='requested'))
      AND ('${behavior_period}'='all' OR requested IS NULL
      OR (julianday(requested)>=julianday(${__from}/1000,'unixepoch') AND julianday(requested)<julianday(${__to}/1000,'unixepoch')))),
    adventure_latest AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY island,flow ORDER BY CAST(json_extract(payload_json,'$.revision') AS INTEGER) DESC,julianday(occurred_at) DESC) rn
      FROM business_history WHERE event_type='ai_adventure_state' AND (island,flow) IN (SELECT island,flow FROM flows)),
    words AS (SELECT island,entry,MIN(CASE WHEN event_type='island_lexicon_result' AND result='created' THEN occurred_at END) created,
      MIN(CASE WHEN event_type='island_lexicon_used' THEN occurred_at END) used,MAX(json_extract(payload_json,'$.content')) content,
      MAX(json_extract(payload_json,'$.category')) category,SUM(event_type='island_lexicon_used') uses
      FROM business WHERE event_type IN ('island_lexicon_result','island_lexicon_used') AND entry IS NOT NULL GROUP BY island,entry),
    requests AS (SELECT island,user_id,json_extract(payload_json,'$.request_id') request_id,
      MAX(json_extract(payload_json,'$.business_group')) business_group,MAX(flow) flow,
      MAX(phase='completed') completed,MAX(phase='failed') failed,MAX(phase='cancelled') cancelled,
      MAX(json_extract(payload_json,'$.elapsed_ms')) elapsed_ms,
      MAX(json_extract(payload_json,'$.estimated_usd')) usd,
      MAX(json_extract(payload_json,'$.cost_is_upper_bound')) upper_bound,
      MAX(json_extract(payload_json,'$.ttfb_ms')) ttfb_ms,
      MAX(json_extract(payload_json,'$.response_stats.prompt_tokens')) input_tokens,
      MAX(json_extract(payload_json,'$.response_stats.cached_tokens')) cached_tokens
      FROM business WHERE event_type='ai_business_request' AND json_extract(payload_json,'$.request_id') IS NOT NULL
      GROUP BY island,user_id,request_id) """
    def p(pid,title,sql,description):
        return q(pid,title,prefix+sql,description+' 缺失不补零；沿用玩家、Playtest、平台、版本和行为时间筛选。')
    coverage="SELECT event_type 事件类型,COUNT(*) 事件数,COUNT(DISTINCT user_id) 玩家数,MIN(occurred_at) 首条采集,MAX(occurred_at) 最近采集 FROM business WHERE event_type IN ('theater_lifecycle','theater_line','theater_view','theater_choice','island_lexicon_result','island_lexicon_used','ai_building_generation','ai_building_created','ai_adventure_state','ai_business_request','ai_adventure_action') GROUP BY event_type"
    return [
      p(300,'AI／小剧场采集覆盖',coverage,'新生命周期指标从新版采集开始；仅有邀请／台词的历史不能推算发起与完成。'),
      p(301,'小剧场场次汇总',"""SELECT CASE WHEN EXISTS(SELECT 1 FROM business WHERE event_type IN ('theater_lifecycle','ai_adventure_state')) THEN COUNT(CASE WHEN offered IS NOT NULL THEN 1 END) END 已记录发起,
        COUNT(CASE WHEN started IS NOT NULL THEN 1 END) 已记录开演,CASE WHEN EXISTS(SELECT 1 FROM business WHERE event_type IN ('theater_lifecycle','ai_adventure_state')) THEN COUNT(CASE WHEN completed IS NOT NULL THEN 1 END) END 已记录完成,
        COUNT(CASE WHEN started IS NOT NULL AND completed IS NULL AND terminal IS NULL THEN 1 END) 未观察到终态,
        COUNT(CASE WHEN offered IS NULL THEN 1 END) 缺发起记录场次,
        CASE WHEN EXISTS(SELECT 1 FROM business WHERE event_type IN ('theater_lifecycle','ai_adventure_state')) THEN ROUND(100.0*SUM(offered IS NOT NULL AND started IS NOT NULL AND completed IS NOT NULL)/NULLIF(SUM(offered IS NOT NULL AND started IS NOT NULL),0),2) END 开演完成率
        FROM flows""",'按存档和剧情 ID 去重，包含建筑冒险；期间模式按发起时间选场次，关联截至所选结束时的结果。无发起证据的历史单列且不进入完成率分母。'),
      p(302,'小剧场 · 类别与参演猫数',"""SELECT COALESCE(business_group,'未采集') 业务组,template 类型,
        CASE WHEN template='pet_truth_question' THEN '不适用（问答）'
          WHEN actual_actors IS NULL AND started IS NULL THEN '尚未观察到开演'
          WHEN actual_actors IS NULL THEN '未采集' WHEN actual_actors>=4 THEN '4只及以上' ELSE CAST(actual_actors AS TEXT)||'只' END 实际参演猫数,
        COUNT(*) 观察到场次,CASE WHEN MAX(lifecycle)=1 THEN SUM(offered IS NOT NULL) END 已记录发起,
        SUM(started IS NOT NULL) 已记录开演,CASE WHEN MAX(lifecycle)=1 THEN SUM(completed IS NOT NULL) END 已记录完成
        FROM flows GROUP BY 1,2,3 ORDER BY 观察到场次 DESC""",'猫数来自实际播放请求演员集合；不是携带猫数。缺真实演员记录的旧场次保留未知。'),
      p(303,'小剧场 · 邀请曝光与玩家参与',"""SELECT template 类型,COUNT(DISTINCT CASE WHEN event_type='theater_view' THEN user_id END) 曝光玩家,
        COUNT(DISTINCT CASE WHEN event_type='theater_choice' AND json_extract(payload_json,'$.choice_class')='continue' THEN user_id END) 选择继续玩家,
        COUNT(DISTINCT CASE WHEN event_type='theater_view' AND json_extract(payload_json,'$.playback_started')=1 THEN user_id END) 观察到播放玩家,
        COUNT(DISTINCT CASE WHEN event_type='theater_choice' AND json_extract(payload_json,'$.choice_class')='continue' THEN json_array(user_id,island,flow) END) 玩家参与场次
        FROM business WHERE event_type IN ('theater_view','theater_choice') GROUP BY template""",'点击继续是参与意图，客户端播放状态是观察证据，二者分别展示；不宣称玩家看完。'),
      p(304,'小剧场 · 退出原因与观看时长',"""SELECT template 类型,reason 原因,phase 阶段,COUNT(*) 退出次数,
        COUNT(DISTINCT user_id) 玩家数,ROUND(AVG(json_extract(payload_json,'$.elapsed_seconds')),2) 平均观看秒
        FROM business WHERE event_type='theater_exit' GROUP BY 1,2,3 ORDER BY 退出次数 DESC""",'Esc 是本地退出，不作为服务端剧情失败；未收到退出记录不推算退出。'),
      p(305,'岛屿词汇 · 提交、入库与使用',"""SELECT
        (SELECT COUNT(*) FROM (SELECT DISTINCT user_id,session_id,json_extract(payload_json,'$.decision_id') FROM business WHERE event_type='theater_choice' AND json_extract(payload_json,'$.action')='submit_draft_text' AND TRIM(COALESCE(json_extract(payload_json,'$.content'),''))<>'')) 文本提交次数,
        (SELECT COUNT(DISTINCT user_id) FROM business WHERE event_type='theater_choice' AND json_extract(payload_json,'$.action')='submit_draft_text') 提交玩家数,
        (SELECT SUM(created IS NOT NULL) FROM words) 新增词条数,
        (SELECT COUNT(*) FROM business WHERE event_type='island_lexicon_result' AND result='reused') 复用操作数,
        (SELECT SUM(uses) FROM words) 实际使用次数,(SELECT SUM(created IS NOT NULL AND used IS NOT NULL) FROM words) 新增后已使用词条,
        (SELECT SUM(created IS NOT NULL AND used IS NULL) FROM words) 新增后未观察到使用词条""",'提交按决策 ID 去重，分类点击不重复算提交；词汇使用来自共享使用记录器。未观察到使用不代表永远不会使用。'),
      p(306,'岛屿词汇 · 入库分类与来源',"SELECT json_extract(payload_json,'$.category') 分类,result 入库结果,COUNT(*) 操作次数,COUNT(DISTINCT user_id) 玩家数 FROM business WHERE event_type='island_lexicon_result' GROUP BY 1,2",'成功新增与重复复用分列；原问题和其他占比仍见现有小剧场问题分类面板。'),
      p(307,'岛屿词汇 · 实际使用明细',"SELECT content 词汇,category 分类,created 创建时间,used 首次观察到使用,uses 使用次数,ROUND((julianday(used)-julianday(created))*86400,2) 首次使用等待秒,CASE WHEN used IS NULL THEN '尚未观察到使用' WHEN created IS NULL THEN '缺创建记录，无法计算等待时间' ELSE '已观察到使用' END 采集状态 FROM words ORDER BY uses DESC LIMIT 200",'只用明确关联的词条 ID；没有创建记录时不推算等待时间。'),
      p(308,'AI 建筑 · 生成与建成',"""SELECT CASE WHEN COUNT(*)>0 THEN COUNT(*) END 生成流程数,COUNT(DISTINCT user_id) 生成玩家数,SUM(succeeded) 生成成功,SUM(failed) 生成失败,SUM(cancelled) 取消,
        SUM(succeeded=0 AND failed=0 AND cancelled=0) 未观察到结果,
        (SELECT COUNT(*) FROM (SELECT DISTINCT island,json_extract(payload_json,'$.building_instance_id') FROM business WHERE event_type='ai_building_created')) 新建建筑实例,
        (SELECT SUM(json_extract(payload_json,'$.money_spent')) FROM business WHERE event_type='ai_building_created') 新建花费金币
        FROM generation""",'新流程 ID 口径；建筑实例仅在保存成功后首次放置计数。历史生成与放置另表展示，不能混用。'),
      p(309,'AI 建筑 · 失败原因与生成耗时',"SELECT phase 结果,reason 原因,COUNT(*) 次数,ROUND(AVG(json_extract(payload_json,'$.elapsed_seconds')),2) 平均秒 FROM business WHERE event_type='ai_building_generation' AND phase IN ('completed','failed','cancelled') GROUP BY 1,2 ORDER BY 次数 DESC",'从真正请求开始计时；缺少终态的任务不算失败。'),
      p(310,'AI 建筑 · 历史生成与放置记录',"""SELECT event_type 记录类型,json_extract(payload_json,'$.status') 状态,COUNT(*) 记录数,COUNT(DISTINCT user_id) 玩家数
        FROM business WHERE event_type IN ('ai_generated_building_wish','ai_generation_completed','ai_generation_failed','ai_generated_furniture_feedback')
        OR event_type='building_place' AND json_extract(payload_json,'$.is_ai_generated')=1 GROUP BY 1,2 ORDER BY 记录数 DESC""",'原始记录次数而非去重生成任务数；completed 中 placed 只表示放置，不作为再次生成成功。'),
      p(311,'建筑冒险 · 主题与当前结果',"""SELECT COALESCE(json_extract(payload_json,'$.theme_name'),'未采集') 主题,
        json_extract(payload_json,'$.initiation_mode') 发起方,actors 参演猫数,phase 当前观察状态,
        COUNT(*) 冒险数,COUNT(DISTINCT user_id) 玩家数,SUM(json_extract(payload_json,'$.reward_granted')) 已授予奖励数
        FROM adventure_latest WHERE rn=1 GROUP BY 1,2,3,4 ORDER BY 冒险数 DESC""",'每个冒险 ID 只取最新保存状态，暂停、重试和恢复不增加冒险数；终态与奖励以保存成功为准。'),
      p(312,'建筑冒险 · 阶段与错误',"""SELECT phase 阶段,json_extract(payload_json,'$.error') 错误,reason 终止原因,
        COUNT(*) 状态变更次数,COUNT(DISTINCT json_array(island,flow)) 涉及冒险数,COUNT(DISTINCT user_id) 玩家数
        FROM business WHERE event_type='ai_adventure_state' GROUP BY 1,2,3 ORDER BY 状态变更次数 DESC""",'状态变化次数用于定位阶段问题，不当作新的冒险场次。'),
      p(313,'AI／小剧场业务明细',"""SELECT occurred_at 时间,user_id,event_type 类型,
        CASE WHEN event_type='island_lexicon_used' AND flow IS NULL THEN '不适用' ELSE flow END 流程ID,
        CASE WHEN event_type IN ('theater_lifecycle','ai_adventure_state') THEN template ELSE '不适用' END 剧情类型,
        CASE WHEN event_type IN ('island_lexicon_result','island_lexicon_used','ai_building_created') THEN '不适用' ELSE phase END 阶段,
        CASE WHEN event_type IN ('theater_lifecycle','ai_adventure_state') THEN actors ELSE '不适用' END 参演猫数,
        payload_json payload FROM business WHERE event_type IN ('theater_lifecycle','island_lexicon_result','island_lexicon_used','ai_building_generation','ai_building_created','ai_adventure_state') ORDER BY julianday(occurred_at) DESC LIMIT 200""",'保留完整 payload，可对照游戏中的行为与最终结果；不属于该事件契约的字段显示不适用。'),
      p(314,'AI · 业务调用、耗时与已知费用',"""SELECT COALESCE(business_group,'未关联') 业务,COUNT(*) 请求数,SUM(completed) 完成请求,SUM(failed) 失败请求,SUM(cancelled) 取消请求,
        SUM(completed=0 AND failed=0 AND cancelled=0) 尚无结果,SUM(usd) 已知费用USD,SUM(completed=1 AND usd IS NULL) 缺计价请求数,
        SUM(upper_bound=1) 费用上限请求数,ROUND(AVG(elapsed_ms),2) 平均全程毫秒,ROUND(AVG(ttfb_ms),2) 平均首字节毫秒,
        ROUND(100.0*SUM(cached_tokens)/NULLIF(SUM(CASE WHEN cached_tokens IS NOT NULL THEN input_tokens END),0),2) 已回报缓存命中率,
        SUM(NULLIF(flow,'') IS NULL) 缺流程关联请求数
        FROM requests GROUP BY 1""",'统一网关新采集口径，不与旧 CatAgent 费用卡相加。完整耗时包含排队，TTFB 独立；图片服务缺真实价格不估价。'),
      p(315,'耗时分布 · 中位数与 P90',""", samples AS (
        SELECT '建筑生成' metric,seconds v FROM generation WHERE succeeded=1
        UNION ALL SELECT '小剧场完成历时', (julianday(completed)-julianday(started))*86400 FROM flows WHERE completed IS NOT NULL AND started IS NOT NULL
        UNION ALL SELECT '词汇首次使用等待',(julianday(used)-julianday(created))*86400 FROM words WHERE used IS NOT NULL AND created IS NOT NULL
        UNION ALL SELECT 'AI请求全程',elapsed_ms/1000.0 FROM requests WHERE completed=1
        UNION ALL SELECT '玩家观看退出',json_extract(payload_json,'$.elapsed_seconds') FROM business WHERE event_type='theater_exit'
        ), ranked AS (SELECT metric,v,ROW_NUMBER() OVER(PARTITION BY metric ORDER BY v) rn,COUNT(*) OVER(PARTITION BY metric) n FROM samples WHERE v>=0)
        SELECT metric 指标,MAX(n) 样本数,ROUND(AVG(CASE WHEN rn IN ((n+1)/2,(n+2)/2) THEN v END),2) 中位数秒,
        ROUND(MAX(CASE WHEN rn=CAST((9*n+9)/10 AS INTEGER) THEN v END),2) P90秒 FROM ranked GROUP BY metric""",'完成历时包含暂停，只显示可靠起止时间的样本；不是活跃游玩时长。'),
      p(316,'建筑冒险 · 操作与重复参与',"""SELECT json_extract(payload_json,'$.action') 操作,
        COUNT(DISTINCT json_array(user_id,session_id,json_extract(payload_json,'$.request_id'))) 操作次数,
        COUNT(DISTINCT json_array(island,flow)) 冒险数,COUNT(DISTINCT user_id) 玩家数
        FROM business WHERE event_type='ai_adventure_action' GROUP BY 1""",'服务器接受后的选择、自由输入、暂停和恢复；网络重传按请求 ID 去重。'),
      p(317,'AI 建筑 · 生成后的实际放置',"""SELECT COUNT(*) 成功且可关联生成,
        SUM(EXISTS(SELECT 1 FROM business b WHERE b.event_type='ai_building_created' AND b.island=generation.island AND json_extract(b.payload_json,'$.source_id')=generation.source_id)) 已观察到放置,
        ROUND(100.0*SUM(EXISTS(SELECT 1 FROM business b WHERE b.event_type='ai_building_created' AND b.island=generation.island AND json_extract(b.payload_json,'$.source_id')=generation.source_id))/NULLIF(COUNT(*),0),2) 放置比例
        FROM generation WHERE succeeded=1 AND NULLIF(source_id,'') IS NOT NULL""",'仅关联明确 source_id 的成功生成；缺来源的历史不进入分母。'),
      p(318,'AI 建筑 · 玩家反馈',"SELECT json_extract(payload_json,'$.feedback') 反馈,COUNT(*) 次数,COUNT(DISTINCT user_id) 玩家数 FROM business WHERE event_type='ai_generated_furniture_feedback' GROUP BY 1 ORDER BY 次数 DESC",'展示真实反馈类型；不把没有反馈的建筑推算为不喜欢。'),
      p(319,'AI · 每个完成流程的已知费用',""", totals AS (SELECT island,flow,SUM(usd) usd,SUM(completed=1 AND usd IS NULL) unknown FROM requests WHERE NULLIF(flow,'') IS NOT NULL GROUP BY island,flow)
        SELECT f.template 类型,COUNT(*) 有调用关联的完成场次,SUM(t.usd) 已知费用USD,AVG(t.usd) 每场平均已知费用USD,SUM(t.unknown) 缺计价调用
        FROM flows f JOIN totals t ON f.island=t.island AND f.flow=t.flow WHERE f.completed IS NOT NULL GROUP BY f.template""",'只统计明确流程关联且已完成的样本；缺费用数量与已知小计同时展示。'),
      p(320,'岛屿词汇 · 问题与其他分类占比',""", choices AS (
        SELECT DISTINCT user_id,session_id,json_extract(payload_json,'$.decision_id') decision_id,template,
        json_extract(payload_json,'$.prompt_text') question,json_extract(payload_json,'$.vocab_type') category
        FROM business WHERE event_type='theater_choice' AND phase='vocab_category' AND json_extract(payload_json,'$.action')='pick_draft_type')
        SELECT template 类型,question 原问题,COUNT(*) 分类选择次数,COUNT(DISTINCT user_id) 玩家数,
        SUM(category='person') 人物,SUM(category='activity') 活动,SUM(category='object') 物品,SUM(category='other') 其他,
        ROUND(100.0*SUM(category='other')/COUNT(*),2) 其他占比 FROM choices GROUP BY 1,2 ORDER BY 分类选择次数 DESC""",'仅统计明确分类操作；默认 Other 不视为玩家选择，按决策 ID 去重。'),
      p(321,'建筑冒险 · 玩家参与频次',""", participation AS (
        SELECT user_id,COUNT(*) n,SUM(phase='completed') completed FROM adventure_latest WHERE rn=1 GROUP BY user_id)
        SELECT COUNT(*) 发起玩家数,SUM(n>=2) 多次冒险玩家,ROUND(100.0*SUM(n>=2)/NULLIF(COUNT(*),0),2) 多次冒险占比,
        SUM(n) 冒险总数,ROUND(AVG(n),2) 人均冒险数,SUM(completed) 已完成冒险 FROM participation""",'按服务端归属玩家统计发起频次，不把围观玩家计为发起者；恢复原冒险不增加次数。'),
      p(322,'建筑冒险 · 各阶段已观察历时',""", intervals AS (
        SELECT phase,julianday(occurred_at) since,LEAD(julianday(occurred_at)) OVER(PARTITION BY island,flow ORDER BY CAST(json_extract(payload_json,'$.revision') AS INTEGER),julianday(occurred_at)) until
        FROM business_history WHERE event_type='ai_adventure_state' AND (island,flow) IN (SELECT island,flow FROM flows))
        SELECT phase 阶段,COUNT(*) 完整区间数,ROUND(SUM((until-since)*86400),2) 累计秒,
        ROUND(AVG((until-since)*86400),2) 平均秒 FROM intervals WHERE until>=since GROUP BY phase""",'只计算相邻已保存状态间隔；暂停独立展示，末尾未结束区间不补时长，不等于玩家活跃时长。'),
    ]
