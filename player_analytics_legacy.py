"""Restore the legacy overview's operational fields using the current filter contract."""
import copy
import json
from pathlib import Path


def restore_legacy_content(boards, panel, scope, time_range):
    """Reuse the legacy report columns, but deduplicate snapshots and filter actual origins."""
    legacy = json.loads((Path(__file__).parent / 'grafana/provisioning/dashboards/gameplay-overview-legacy.json').read_text())
    old = {p['id']: p for p in legacy['panels']}
    overview = boards['gameplay-overview.json']
    period = lambda column: "('${behavior_period}'='all' OR (" + time_range(column) + '))'
    prefix = f"""WITH session_source AS (
        SELECT g.source_file,g.user_id,g.session_id,g.nickname,g.client_platform,g.client_version,g.release_version,g.is_development_build,
            g.real_time_started_iso,g.game_day_start,g.game_day_end,g.ai_usage_source,g.ai_response_count,g.ai_token_record_count,
            g.ai_total_tokens,g.ai_input_tokens,g.ai_output_tokens,g.ai_cached_input_tokens,g.ai_cache_hit_ratio,g.ai_estimated_cost_usd,g.ai_models,
            COALESCE(u.is_developer,0) is_developer,
            ROW_NUMBER() OVER(PARTITION BY g.user_id,g.session_id ORDER BY g.imported_at DESC,g.source_file DESC) snapshot_rank
        FROM gameplay_sessions g LEFT JOIN user_sessions u ON u.user_id=g.user_id),
    selected_sessions AS (SELECT * FROM session_source WHERE snapshot_rank=1 AND {scope} AND {period('real_time_started_iso')}),
    selected_users AS (SELECT DISTINCT user_id FROM analytics_sessions WHERE {scope}),
    chat_source AS (SELECT c.*,COALESCE(u.is_developer,0) is_developer FROM conversations c LEFT JOIN user_sessions u ON u.user_id=c.user_id),
    selected_chats AS (SELECT * FROM chat_source WHERE {scope} AND {period('timestamp')}),
    active_chat_stats AS (SELECT user_id,COUNT(*) active_chat_count FROM selected_chats
        WHERE COALESCE(NULLIF(message_type,''),'chat')='chat' AND COALESCE(user_query,'')<>'' AND COALESCE(is_preset,0)=0 GROUP BY user_id),
    call_source AS (SELECT c.user_id,c.session_id,c.client_platform,c.client_version,c.release_version,c.is_development_build,
        c.event_real_time_iso,c.model,c.total_tokens,c.input_tokens,c.output_tokens,c.cached_input_tokens,c.estimated_cost_usd,
        COALESCE(u.is_developer,0) is_developer,
        ROW_NUMBER() OVER(PARTITION BY c.user_id,c.session_id,c.game_day,c.event_index ORDER BY c.imported_at DESC,c.source_file DESC) call_rank
        FROM gameplay_ai_calls c LEFT JOIN user_sessions u ON u.user_id=c.user_id),
    selected_calls AS (SELECT * FROM call_source WHERE call_rank=1 AND {scope} AND {period('event_real_time_iso')}) """
    for legacy_id in range(20, 30):
        source = old[legacy_id]
        # These legacy reports end their CTEs with ') SELECT'; only their result columns
        # are reused. Filtering and snapshot selection belong to the shared prefix above.
        select = 'SELECT ' + source['targets'][0]['queryText'].rsplit(') SELECT ', 1)[1]
        select = select.replace('COALESCE(us.is_developer, 0) = 0 AND ', '')
        if legacy_id == 27:
            select = select.replace("date(COALESCE(event_real_time_iso, imported_at))", "COALESCE(date(event_real_time_iso,'+8 hours'),'未采集日期')")
        description = '恢复旧版 AI/玩家运营字段，沿用平台、版本和测试数据筛选。AI 总计取去重会话快照，模型/每日表取去重调用明细，来源覆盖可能不同；成本为估算值。行为范围默认累计。缺真实时间不推算为导入当天。'
        restored = panel(legacy_id, source['title'], prefix + select, kind=source['type'], description=description)
        restored['fieldConfig']['defaults'].update(noValue='未采集', unit='none')
        if legacy_id in (22,):
            restored['fieldConfig']['defaults'].update(unit='currencyUSD', decimals=6)
        if legacy_id == 23:
            restored['fieldConfig']['defaults'].update(unit='percent', decimals=2)
        if restored['type'] == 'table':
            restored['fieldConfig']['defaults']['custom'].update(inspect=True,minWidth=150)
        overview['panels'].append(restored)

    # Preserve the old module/action/detail breakdown as an explicitly sourced report.
    core = """WITH selected_sessions AS (
        SELECT g.source_file,g.user_id,g.session_id,g.client_platform,g.client_version,g.release_version,g.is_development_build,
            COALESCE(u.is_developer,0) is_developer,
            ROW_NUMBER() OVER(PARTITION BY g.user_id,g.session_id ORDER BY g.imported_at DESC,g.source_file DESC) snapshot_rank
        FROM gameplay_sessions g LEFT JOIN user_sessions u ON u.user_id=g.user_id),
    selected AS (SELECT * FROM selected_sessions WHERE snapshot_rank=1 AND """ + scope + """),
    behavior_events AS (SELECT b.behavior_module,b.behavior_action,
        CASE WHEN b.behavior_module IN ('和猫说话','购买升级项') THEN COALESCE(NULLIF(b.behavior_detail,''),'-') ELSE '-' END behavior_detail,b.user_id
        FROM gameplay_behavior_events b JOIN selected s ON s.source_file=b.source_file AND s.user_id=b.user_id AND s.session_id=b.session_id
        WHERE b.is_behavior_stat=1 AND """ + period('b.event_real_time_iso') + """)
    SELECT behavior_module 模块,behavior_action 行为,behavior_detail 细分,COUNT(*) 次数,COUNT(DISTINCT user_id) 用户数
    FROM behavior_events GROUP BY 1,2,3 ORDER BY 次数 DESC LIMIT 50"""
    overview['panels'].append(panel(14, '核心行为统计', core,
        description='旧版模块/行为/细分口径，来自去重的已导入会话明细；实时增量行为仍见玩家行为 Top 5。'))
    listing = copy.deepcopy(next(p for p in boards['gameplay-players.json']['panels'] if p['id'] == 100))
    listing.update(id=19, title='玩家列表')
    overview['panels'].append(listing)
    # Sorting and paging remain shared with the dedicated player list.
    for variable in overview['templating']['list']:
        if variable['name'] in ('sort_field', 'sort_direction', 'page'):
            variable['hide'] = 0

