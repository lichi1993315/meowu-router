"""Grafana 「玩法模块体验」: 每个模块回答「玩家喜不喜欢、为什么」。

只读 module_analytics 的投影表；少量低频事件（菜单、货架、小剧场退出）按事件类型和时间
走 analytics_event_facts 的 (event_type, occurred_at) 索引。口径见 PawFishing
Docs/Design/AI/ModuleEngagementTelemetry.md §4.6、ModuleDesignQuestions.md。
"""
from module_analytics import MODULES, TIME_COLUMNS, label_sql

DESCRIPTION = ('只统计上报了模块信息的新版客户端（modules-v1）；旧版本没有这些数据，显示为空而不是 0。'
               '「去掉引导」时排除新手链正在要求该模块动作期间的操作。猫和系统自动做的事不算玩家动作。'
               '主动分钟 = 15 秒操作窗口内按「最近模块动作 → 最上层窗口 → 自由走动」归属的估算值。'
               '日期按北京时间；复用/一次性指标按首次使用落在所选时间范围内的玩家计算，后续使用读到现在，未满 7 天的不计入分母。')

# 玩家主动做的模块动作：排除曝光、流水、窗口进出和系统/猫事件。
NON_ACTION_KINDS = ("'offer_shown','currency_delta','ui_view_opened','ui_view_closed','feature_entry_exposed',"
                    "'theater_line','theater_view','restaurant_guest_ordered','restaurant_guest_left_angry',"
                    "'mail_arrived','visitor_wishes_appeared','restaurant_session_ended'")
PLAYER_ACT = (f"is_player=1 AND COALESCE(source,'player') NOT IN ('cat','system','automatic','server_playback') "
              f"AND event_type NOT IN ({NON_ACTION_KINDS})")
USED = PLAYER_ACT + " AND COALESCE(result,'success') IN ('success','started')"
GUIDED = "('${module_guided}'='include' OR COALESCE(guided,0)=0)"
MODULE = label_sql('module_id')
MODULE_VALUES = 'modules(module_id,sort) AS (VALUES ' + ','.join(f"('{m}',{i})" for i, m in enumerate(MODULES)) + ')'


def _scoped(name, table, scope, time_range, column='occurred_at', alias='t'):
    """先挂开发者标记，再套平台/版本/测试/批次/渠道筛选（这些筛选写的是不带表前缀的列名）。"""
    timed = f' AND {time_range(alias + "." + column)}' if time_range else ''
    return (f"{name}_raw AS (SELECT {alias}.*,COALESCE(u.is_developer,0) is_developer FROM {table} {alias} "
            f"LEFT JOIN user_sessions u ON u.user_id={alias}.user_id WHERE 1{timed}), "
            f"{name} AS (SELECT * FROM {name}_raw WHERE {scope})")


def _time(scope, time_range):
    """按 run 相邻检查点差值还原每个模块的主动秒数；差值在整段 run 上算，再按时间裁剪，避免把区间外累计算进来。"""
    lags = ','.join(f'LAG({c}) OVER w prev_{c}' for c in TIME_COLUMNS)
    sums = ','.join(f'SUM(MAX(0,{c}-COALESCE(prev_{c},0))) {c}' for c in TIME_COLUMNS)
    unpivot = ' UNION ALL '.join(f"SELECT user_id,'{c}' module_id,{c} seconds FROM tsum" for c in TIME_COLUMNS)
    return (f"truns AS (SELECT DISTINCT run_id FROM module_time_points WHERE {time_range('occurred_at')}), "
            f"tp_raw AS (SELECT p.*,COALESCE(u.is_developer,0) is_developer,{lags} FROM module_time_points p "
            f"LEFT JOIN user_sessions u ON u.user_id=p.user_id WHERE p.run_id IN (SELECT run_id FROM truns) "
            f"WINDOW w AS (PARTITION BY p.run_id ORDER BY p.monotonic)), "
            f"tp AS (SELECT * FROM tp_raw WHERE {time_range('occurred_at')} AND {scope}), "
            f"tsum AS (SELECT user_id,{sums} FROM tp GROUP BY user_id), "
            f"tlong AS ({unpivot})")


def _facts(name, kinds, scope, time_range):
    return (f"{name}_raw AS (SELECT f.*,COALESCE(u.is_developer,0) is_developer FROM analytics_event_facts f "
            f"LEFT JOIN user_sessions u ON u.user_id=f.user_id WHERE f.event_type IN ({kinds}) AND {time_range('f.occurred_at')}), "
            f"{name} AS (SELECT * FROM {name}_raw WHERE {scope})")


def panels(scope, time_range):
    m = _scoped('m', 'module_events', scope, time_range)
    m_all = _scoped('ma', 'module_events', scope, None)
    overview = f"""WITH {m}, {_time(scope, time_range)}, {MODULE_VALUES},
      acts AS (SELECT * FROM m WHERE {PLAYER_ACT} AND {GUIDED}),
      a AS (SELECT module_id,COUNT(DISTINCT user_id) tried,
        COUNT(DISTINCT CASE WHEN COALESCE(result,'success') IN ('success','started') THEN user_id END) used,
        COUNT(*) n,COUNT(DISTINCT user_id||'|'||local_day) user_days,
        SUM(result='rejected') rejected,SUM(result IN ('failed','cancelled')) failed FROM acts GROUP BY module_id),
      seen AS (SELECT module_id,COUNT(DISTINCT CASE WHEN event_type='feature_entry_exposed' THEN user_id END) exposed,
        COUNT(DISTINCT CASE WHEN event_type='ui_view_opened' THEN user_id END) opened FROM m
        WHERE event_type IN ('feature_entry_exposed','ui_view_opened') GROUP BY module_id),
      t AS (SELECT module_id,SUM(seconds) seconds,COUNT(DISTINCT CASE WHEN seconds>0 THEN user_id END) players FROM tlong GROUP BY module_id),
      total AS (SELECT SUM(seconds) s FROM t WHERE module_id!='free_roam'),
      x AS (SELECT module_id,sort FROM modules UNION ALL SELECT 'free_roam',999)
      SELECT {label_sql('x.module_id')} 模块,seen.exposed 看到入口人数,seen.opened 打开窗口人数,a.tried 尝试人数,a.used 成功使用人数,
        a.n 动作次数,ROUND(1.0*a.user_days/NULLIF(a.tried,0),2) 人均使用天数,
        ROUND(100.0*a.rejected/NULLIF(a.n,0),1) 被拒率,ROUND(100.0*a.failed/NULLIF(a.n,0),1) 失败或放弃率,
        ROUND(t.seconds/60,1) 主动分钟,ROUND(t.seconds/60/NULLIF(t.players,0),1) 人均主动分钟,
        ROUND(100.0*t.seconds/NULLIF((SELECT s FROM total),0),1) 时长占比
      FROM x LEFT JOIN a USING(module_id) LEFT JOIN seen USING(module_id) LEFT JOIN t USING(module_id)
      ORDER BY 主动分钟 DESC NULLS LAST,x.sort"""

    liking = f"""WITH {m_all}, {MODULE_VALUES},
      used AS (SELECT user_id,module_id,occurred_at,local_day,julianday(occurred_at) j,
        MIN(julianday(occurred_at)) OVER (PARTITION BY user_id,module_id) first_j FROM ma WHERE {USED} AND {GUIDED}),
      scored AS (SELECT user_id,module_id,MIN(occurred_at) first_at,julianday('now')>=MIN(first_j)+7 matured,
        SUM(j<first_j+7) uses7,COUNT(DISTINCT CASE WHEN j<first_j+7 THEN local_day END) days7,
        MAX(j>=first_j+1 AND j<first_j+7) came_back
        FROM used GROUP BY user_id,module_id HAVING {time_range('MIN(occurred_at)')})
      SELECT {label_sql('s.module_id')} 模块,COUNT(*) 首次使用人数,SUM(matured) 已满7天人数,
        ROUND(100.0*SUM(CASE WHEN matured THEN came_back END)/NULLIF(SUM(matured),0),1) 第2到7天回来再用率,
        ROUND(100.0*SUM(CASE WHEN matured THEN uses7=1 END)/NULLIF(SUM(matured),0),1) 只用一次比例,
        ROUND(AVG(CASE WHEN matured THEN days7 END),2) 七天内人均使用天数,
        ROUND(AVG(CASE WHEN matured THEN uses7 END),1) 七天内人均次数
      FROM scored s JOIN modules USING(module_id) GROUP BY s.module_id ORDER BY 首次使用人数 DESC"""

    views = f"""WITH {m}
      SELECT {MODULE} 模块,view_id 窗口,COUNT(*) 打开次数,COUNT(DISTINCT user_id) 打开人数,
        ROUND(100.0*SUM(open_trigger='player_input')/COUNT(*),1) 玩家点开占比,
        ROUND(AVG(effective_s),1) 平均有效停留秒,
        ROUND(100.0*SUM(open_trigger='player_input' AND COALESCE(action_count,0)=0)/NULLIF(SUM(open_trigger='player_input'),0),1) 点开后没操作就关比例,
        ROUND(AVG(CASE WHEN open_trigger='player_input' THEN action_count END),2) 每次打开平均动作数
      FROM m WHERE event_type='ui_view_closed' GROUP BY module_id,view_id ORDER BY 打开次数 DESC LIMIT 200"""

    blocked = f"""WITH {m},
      acts AS (SELECT * FROM m WHERE {PLAYER_ACT} AND {GUIDED}),
      totals AS (SELECT module_id,COUNT(*) n FROM acts GROUP BY module_id)
      SELECT {MODULE} 模块,action 动作,result 结果,COALESCE(reason,'未给原因') 原因,COUNT(*) 次数,COUNT(DISTINCT user_id) 人数,
        ROUND(100.0*COUNT(*)/MAX(t.n),1) 占该模块动作比例
      FROM acts JOIN totals t USING(module_id) WHERE result IN ('rejected','failed','cancelled')
      GROUP BY module_id,action,result,reason ORDER BY 次数 DESC LIMIT 200"""

    e = _scoped('e', 'module_exit_points', scope, time_range)
    e_all = _scoped('ea', 'module_exit_points', scope, None)
    exits = f"""WITH {e}, {e_all},
      ranked AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY run_id ORDER BY julianday(occurred_at) DESC,sequence DESC) n FROM e),
      user_last AS (SELECT user_id,MAX(julianday(occurred_at)) last_at FROM ea GROUP BY user_id),
      last_points AS (SELECT r.*,julianday(r.occurred_at)+0.0007>=l.last_at final_run,
        julianday('now')-julianday(r.occurred_at)>=3 settled FROM ranked r JOIN user_last l USING(user_id) WHERE n=1)
      SELECT CASE WHEN top_module IS NULL THEN '没开窗口（在世界里）' ELSE {label_sql('top_module')} END 退出时所在界面,
        COALESCE(top_view,'') 窗口,COUNT(*) 会话数,COUNT(DISTINCT user_id) 人数,
        SUM(final_run AND settled) 其中是最后一次且已3天未回,
        ROUND(AVG(CASE WHEN view_depth>0 THEN view_depth END),1) 平均窗口层数
      FROM last_points GROUP BY top_module,top_view ORDER BY 会话数 DESC LIMIT 100"""

    last_actions = f"""WITH {e}, {e_all},
      ranked AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY run_id ORDER BY julianday(occurred_at) DESC,sequence DESC) n FROM e),
      user_last AS (SELECT user_id,MAX(julianday(occurred_at)) last_at FROM ea GROUP BY user_id),
      last_points AS (SELECT r.*,julianday(r.occurred_at)+0.0007>=l.last_at final_run,
        julianday('now')-julianday(r.occurred_at)>=3 settled FROM ranked r JOIN user_last l USING(user_id) WHERE n=1)
      SELECT CASE WHEN last_module IS NULL THEN '本次没有模块动作' ELSE {label_sql('last_module')} END 最后动作模块,
        COALESCE(last_action,'') 动作,COALESCE(last_result,'') 结果,COUNT(*) 会话数,
        SUM(final_run AND settled) 其中是最后一次且已3天未回,
        ROUND(AVG(last_seconds_ago)/60,1) 距离退出平均分钟
      FROM last_points GROUP BY last_module,last_action,last_result ORDER BY 会话数 DESC LIMIT 100"""

    bucket = ("CASE WHEN minutes_since_close<10 THEN '0–10 分钟' WHEN minutes_since_close<30 THEN '10–30 分钟' "
              "WHEN minutes_since_close<60 THEN '30–60 分钟' ELSE '60 分钟以上' END")
    after_close = f"""WITH {m}
      SELECT {bucket} 闭店后,{MODULE} 模块,COUNT(*) 动作次数,COUNT(DISTINCT user_id) 人数,
        ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER(PARTITION BY {bucket}),1) 该时段动作占比
      FROM m WHERE {PLAYER_ACT} AND {GUIDED} AND restaurant_phase='closed' AND minutes_since_close IS NOT NULL
      GROUP BY 1,module_id ORDER BY MIN(minutes_since_close),动作次数 DESC"""
    while_open = f"""WITH {m}
      SELECT {MODULE} 模块,COUNT(*) 动作次数,COUNT(DISTINCT user_id) 人数,
        ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER(),1) 营业中动作占比
      FROM m WHERE {PLAYER_ACT} AND {GUIDED} AND restaurant_phase='open' GROUP BY module_id ORDER BY 动作次数 DESC"""

    menu_kinds = "'restaurant_menu_changed','restaurant_session_started','cooking_completed'"
    dishes = f"""WITH {_facts('rf', menu_kinds, scope, time_range)},
      menu AS (SELECT rf.user_id,CAST(json_extract(j.value,'$.recipe_id') AS INTEGER) recipe_id,json_extract(j.value,'$.count') servings
        FROM rf, json_each(rf.payload_json,'$.menu') j WHERE rf.event_type='restaurant_session_started'),
      cooked AS (SELECT user_id,CAST(json_extract(payload_json,'$.recipe_id') AS INTEGER) recipe_id,
        json_extract(payload_json,'$.dish_name') dish_name,json_extract(payload_json,'$.ordered') ordered FROM rf WHERE event_type='cooking_completed'),
      recipes AS (SELECT recipe_id FROM menu UNION SELECT recipe_id FROM cooked)
      SELECT r.recipe_id 菜谱ID,(SELECT MAX(dish_name) FROM cooked c WHERE c.recipe_id=r.recipe_id) 菜名,
        (SELECT COUNT(*) FROM menu x WHERE x.recipe_id=r.recipe_id) 开店上架次数,
        (SELECT COUNT(DISTINCT user_id) FROM menu x WHERE x.recipe_id=r.recipe_id) 上架人数,
        (SELECT ROUND(AVG(servings),1) FROM menu x WHERE x.recipe_id=r.recipe_id) 平均上架份数,
        (SELECT COUNT(*) FROM cooked c WHERE c.recipe_id=r.recipe_id) 做出份数,
        (SELECT SUM(ordered=0) FROM cooked c WHERE c.recipe_id=r.recipe_id) 没人点也做的份数
      FROM recipes r ORDER BY 开店上架次数 DESC,做出份数 DESC LIMIT 100"""

    vending = f"""WITH {_facts('of', "'offer_shown'", scope, time_range)}, {m},
      shown AS (SELECT of.user_id,json_extract(j.value,'$.content_id') content_id,json_extract(j.value,'$.lock_reason') lock_reason,
        json_extract(j.value,'$.price') price FROM of, json_each(of.payload_json,'$.items') j
        WHERE COALESCE(json_extract(of.metadata_json,'$.module_id'),json_extract(of.payload_json,'$.module_id'))=${{offer_module:sqlstring}}),
      bought AS (SELECT user_id,content_id FROM m WHERE module_id=${{offer_module:sqlstring}} AND result='success' AND action IN ('buy','buy_tool','ai_set_buy'))
      SELECT s.content_id 商品,MAX(s.price) 价格,COUNT(*) 曝光次数,COUNT(DISTINCT s.user_id) 曝光人数,
        SUM(s.lock_reason IS NOT NULL) 曝光时买不了次数,
        (SELECT GROUP_CONCAT(DISTINCT x.lock_reason) FROM shown x WHERE x.content_id=s.content_id AND x.lock_reason IS NOT NULL) 买不了的原因,
        (SELECT COUNT(*) FROM bought b WHERE b.content_id=s.content_id) 购买次数,
        (SELECT COUNT(DISTINCT user_id) FROM bought b WHERE b.content_id=s.content_id) 购买人数,
        ROUND(100.0*(SELECT COUNT(DISTINCT user_id) FROM bought b WHERE b.content_id=s.content_id)/COUNT(DISTINCT s.user_id),1) 看过的人里买了的比例
      FROM shown s GROUP BY s.content_id ORDER BY 曝光人数 DESC LIMIT 200"""

    currency = f"""WITH {m}
      SELECT currency 货币,{MODULE} 模块,COALESCE(reason,'') 原因,
        SUM(CASE WHEN delta>0 THEN delta ELSE 0 END) 获得,SUM(CASE WHEN delta<0 THEN -delta ELSE 0 END) 消耗,
        COUNT(DISTINCT user_id) 人数
      FROM m WHERE event_type='currency_delta' GROUP BY currency,module_id,reason ORDER BY currency,消耗 DESC,获得 DESC"""

    theater_kinds = "'theater_exit','theater_line'"
    theater = f"""WITH {_facts('tf', theater_kinds, scope, time_range)},
      exits AS (SELECT json_extract(payload_json,'$.phase') phase,json_extract(payload_json,'$.reason') reason,
        json_extract(payload_json,'$.wait_to_playback_seconds') wait_s,json_extract(payload_json,'$.playback_seconds') play_s,user_id
        FROM tf WHERE event_type='theater_exit'),
      scenes AS (SELECT json_extract(payload_json,'$.theater_event_id') event_id,json_extract(payload_json,'$.scene_id') scene_id,
        MAX(json_extract(payload_json,'$.line_index'))+1 played,MAX(json_extract(payload_json,'$.scene_line_count')) planned
        FROM tf WHERE event_type='theater_line' AND json_extract(payload_json,'$.scene_line_count') IS NOT NULL GROUP BY 1,2)
      SELECT '退出 · '||COALESCE(phase,'?')||' · '||COALESCE(reason,'?') 项目,COUNT(*) 次数,COUNT(DISTINCT user_id) 人数,
        ROUND(AVG(wait_s),1) 选择后等开演平均秒,ROUND(AVG(play_s),1) 平均播放秒,NULL 平均播完比例
      FROM exits GROUP BY phase,reason
      UNION ALL SELECT '分幕播放（服务端实际开播）',COUNT(*),NULL,NULL,NULL,ROUND(100.0*AVG(1.0*played/planned),1) FROM scenes
      ORDER BY 次数 DESC"""

    quality = f"""WITH {m}
      SELECT {MODULE} 模块,COUNT(*) 事件数,COUNT(DISTINCT user_id) 人数,
        SUM(event_type='ui_view_opened') 打开窗口,SUM(event_type='ui_view_closed') 关闭窗口,
        SUM(guided=1) 引导中动作,GROUP_CONCAT(DISTINCT CASE WHEN module_id='unknown' THEN COALESCE(view_id,event_type) END) 未登记来源,
        GROUP_CONCAT(DISTINCT release_version) 版本
      FROM m GROUP BY module_id ORDER BY module_id='unknown' DESC,事件数 DESC"""

    return [
        (1, '模块总览：谁看到、谁打开、谁在玩、玩多久', overview),
        (2, '喜欢信号：首次使用后 7 天内回来再用 / 只用一次', liking),
        (3, '界面：打开后待多久、点开没操作就关的比例', views),
        (4, '想做做不成：被拒、失败、放弃的原因', blocked),
        (5, '退出时所在界面（每次会话最后一个检查点）', exits),
        (6, '退出前最后一个动作', last_actions),
        (7, '闭店后去做什么（按闭店后经过的分钟分段）', after_close),
        (8, '餐厅营业中玩家在做什么', while_open),
        (9, '菜单偏好：开店上架 vs 实际做出（含没人点也做）', dishes),
        (10, '货架曝光 → 购买（模块见右上角「货架模块」）', vending),
        (11, '货币流水（喵露 / 点赞 / 生成券；金币见经营总览）', currency),
        (12, '小剧场：在哪退出、等开演多久、每幕播完比例', theater),
        (13, '数据质量：各模块事件覆盖与未登记来源', quality),
    ]


def extend_dashboards(result, dashboard, panel, scope, time_range):
    board = dashboard('gameplay-module-engagement', '玩法模块体验',
                      [panel(pid, title, sql, description=DESCRIPTION) for pid, title, sql in panels(scope, time_range)])
    board['templating']['list'].extend([
        {'name': 'module_guided', 'label': '新手引导中的操作', 'type': 'custom',
         'query': '去掉引导 : exclude,包含引导 : include', 'current': {'text': '去掉引导', 'value': 'exclude'}},
        {'name': 'offer_module', 'label': '货架模块', 'type': 'custom',
         'query': '售货机 : vending,商店 : shop', 'current': {'text': '售货机', 'value': 'vending'}},
    ])
    for variable in board['templating']['list']:
        if variable['name'] in ('user_id', 'session_id', 'llm_request_id', 'page'):
            variable['hide'] = 2
    board['panels'][0]['options']['content'] = DESCRIPTION
    board['panels'][0]['gridPos']['h'] = 4
    for p in board['panels'][1:]:
        p['gridPos']['y'] += 1
    board['time'] = {'from': 'now-14d', 'to': 'now'}
    result['gameplay-module-engagement.json'] = board
    link = {'title': '玩法模块体验', 'type': 'link', 'url': '/d/gameplay-module-engagement', 'includeVars': True, 'keepTime': True}
    for other in result.values():
        if link not in other.setdefault('links', []):
            other['links'].append(link)
