"""Per-module engagement projections (PawFishing ModuleEngagementTelemetry.md).

Intake writes at most one slim row per event: O(1), no history replay. Dashboards read only these
tables; raw payloads stay in analytics_event_facts as the audit trail. Rows exist only for clients
that stamp module_id (Unity modules-v1); older data is shown as "not collected", never as zero.
"""
import sqlite3
from datetime import datetime, timedelta

# Must match GamePlay/Shared/Player/TelemetryModules.All (order irrelevant here).
MODULES = ('cat_chat', 'theater', 'ai_building', 'cat_petting', 'cat_feeding', 'cat_profile', 'cat_house',
           'fishing', 'farming', 'restaurant', 'bug_catching', 'foraging', 'shop', 'vending', 'wardrobe',
           'building', 'collection', 'research', 'aquarium', 'wish_star', 'visitor_wish', 'mail_notes',
           'explore', 'progress', 'system', 'unknown')
LABELS = {
    'cat_chat': '和猫聊天', 'theater': '小剧场', 'ai_building': 'AI 建筑/冒险', 'cat_petting': '撸猫',
    'cat_feeding': '喂猫', 'cat_profile': '猫档案', 'cat_house': '猫舍/领养', 'fishing': '钓鱼',
    'farming': '种田', 'restaurant': '餐厅', 'bug_catching': '捕虫', 'foraging': '采集', 'shop': '商店',
    'vending': '售货机', 'wardrobe': '衣柜/换装', 'building': '建造装饰', 'collection': '图鉴/喵露册',
    'research': '研究', 'aquarium': '水族馆', 'wish_star': '许愿星', 'visitor_wish': '访客愿望',
    'mail_notes': '信箱/留言', 'explore': '地图/移动', 'progress': '任务/成长', 'system': '系统界面',
    'unknown': '未登记', 'free_roam': '自由走动',
}
TIME_COLUMNS = MODULES + ('free_roam',)
CLIENT_KINDS = frozenset({'ui_view_opened', 'ui_view_closed'})
EXIT_KINDS = frozenset({'journey_checkpoint', 'journey_quit', 'journey_leave', 'journey_background'})


def label_sql(column):
    cases = ' '.join(f"WHEN '{k}' THEN '{v}'" for k, v in LABELS.items())
    return f"CASE {column} {cases} ELSE {column} END"


def ensure_module_schema(conn):
    time_columns = ','.join(c + ' REAL' for c in TIME_COLUMNS)
    for statement in (
        '''CREATE TABLE IF NOT EXISTS module_events(event_id TEXT PRIMARY KEY,user_id TEXT,session_id TEXT,run_id TEXT,
            archive_id TEXT,client_platform TEXT,release_version TEXT,is_development_build INTEGER,occurred_at TEXT,
            local_day TEXT,event_type TEXT,module_id TEXT,action TEXT,result TEXT,reason TEXT,source TEXT,
            is_player INTEGER,guided INTEGER,restaurant_phase TEXT,minutes_since_close REAL,restaurant_session_id TEXT,
            content_id TEXT,view_id TEXT,open_trigger TEXT,effective_s REAL,action_count INTEGER,currency TEXT,delta REAL)''',
        'CREATE INDEX IF NOT EXISTS idx_module_events_module_time ON module_events(module_id,occurred_at)',
        'CREATE INDEX IF NOT EXISTS idx_module_events_user_time ON module_events(user_id,occurred_at)',
        'CREATE INDEX IF NOT EXISTS idx_module_events_kind_time ON module_events(event_type,occurred_at)',
        '''CREATE TABLE IF NOT EXISTS module_time_points(event_id TEXT PRIMARY KEY,user_id TEXT,session_id TEXT,run_id TEXT,
            client_platform TEXT,release_version TEXT,is_development_build INTEGER,occurred_at TEXT,monotonic REAL,'''
        + time_columns + ')',
        'CREATE INDEX IF NOT EXISTS idx_module_time_run ON module_time_points(run_id,monotonic)',
        'CREATE INDEX IF NOT EXISTS idx_module_time_time ON module_time_points(occurred_at)',
        '''CREATE TABLE IF NOT EXISTS module_exit_points(event_id TEXT PRIMARY KEY,user_id TEXT,session_id TEXT,run_id TEXT,
            client_platform TEXT,release_version TEXT,is_development_build INTEGER,occurred_at TEXT,event_type TEXT,
            sequence INTEGER,top_view TEXT,top_module TEXT,view_depth INTEGER,last_module TEXT,last_action TEXT,
            last_result TEXT,last_seconds_ago REAL)''',
        'CREATE INDEX IF NOT EXISTS idx_module_exit_run ON module_exit_points(run_id,occurred_at)',
        'CREATE INDEX IF NOT EXISTS idx_module_exit_time ON module_exit_points(occurred_at)',
    ):
        conn.execute(statement)


def _text(value, limit=128):
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _flag(value):
    return None if value is None else int(bool(value))


def _local_day(occurred):
    """Beijing calendar day; unparsable times stay NULL rather than being guessed."""
    try:
        moment = datetime.fromisoformat(str(occurred).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        return None
    return (moment + timedelta(hours=8)).date().isoformat()


def project_module_event(conn, identity, user, session, event, metadata, release):
    """Project one canonical event. Called from upsert_fact; idempotent by event_id.

    Live intake never runs schema migrations (docs/telemetry-database-maintenance.md). Until the
    maintenance tool has created the module_* tables, skip the projection instead of failing intake;
    raw facts are kept, so a later replay can rebuild the projection.
    """
    try:
        _project(conn, identity, user, session, event, metadata, release)
    except sqlite3.OperationalError as error:
        if 'no such table: module_' not in str(error):
            raise


def _project(conn, identity, user, session, event, metadata, release):
    kind = event.get('event_type') or ''
    payload = event.get('payload') or {}
    if not isinstance(payload, dict):
        return
    platform = metadata.get('client_platform', 'unknown')
    dev = int(bool(metadata.get('is_development_build')))
    occurred = event.get('event_real_time_iso')
    is_journey = kind.startswith('journey_') or kind in CLIENT_KINDS
    # Client facts are keyed by run id; join them to the server session the player was in.
    server_session = payload.get('server_session_id') if is_journey else None
    session_id = server_session or session
    run_id = session if is_journey else None

    participation = payload.get('participation')
    modules = participation.get('modules') if isinstance(participation, dict) else None
    if isinstance(modules, dict) and isinstance(participation.get('monotonic_seconds'), (int, float)):
        values = [_number(modules.get(c)) or 0 for c in TIME_COLUMNS]
        conn.execute(f"INSERT OR REPLACE INTO module_time_points VALUES ({','.join('?' * (9 + len(TIME_COLUMNS)))})",
                     (identity, user, session_id, run_id, platform, release, dev, occurred,
                      participation['monotonic_seconds'], *values))

    if kind in EXIT_KINDS and (payload.get('view_stack') or payload.get('last_action')):
        stack = payload.get('view_stack') if isinstance(payload.get('view_stack'), list) else []
        top = stack[-1] if stack and isinstance(stack[-1], dict) else {}
        last = payload.get('last_action') if isinstance(payload.get('last_action'), dict) else {}
        conn.execute('INSERT OR REPLACE INTO module_exit_points VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (identity, user, session_id, run_id, platform, release, dev, occurred, kind, event.get('sequence'),
                      _text(top.get('view_id')), _text(top.get('module_id')), len(stack), _text(last.get('module_id')),
                      _text(last.get('action')), _text(last.get('result')), _number(last.get('seconds_ago'))))

    module = event.get('module_id') or (payload.get('module_id') if kind in CLIENT_KINDS or kind == 'feature_entry_exposed' else None)
    if module not in MODULES:
        return
    actor = event.get('actor') or {}
    content = payload.get('content_id')
    if content is None:
        for key in ('recipe_id', 'fish_id', 'crop_id', 'item_id', 'seed_id', 'building_id'):
            if payload.get(key) is not None:
                content = f'{key}:{payload.get(key)}'
                break
    conn.execute('INSERT OR REPLACE INTO module_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
        identity, user, session_id, run_id, event.get('archive_id'), platform, release, dev, occurred,
        _local_day(occurred), kind, module, _text(payload.get('action')), _text(payload.get('result')), _text(payload.get('reason')),
        _text(payload.get('source')), 1 if kind in CLIENT_KINDS else _flag(actor.get('is_player')),
        _flag(event.get('guided')), _text(event.get('restaurant_phase')), _number(event.get('minutes_since_close')),
        _text(event.get('restaurant_session_id') or payload.get('restaurant_session_id')), _text(content),
        _text(payload.get('view_id')), _text(payload.get('open_trigger')), _number(payload.get('effective_s')),
        _number(payload.get('action_count')), _text(payload.get('currency')), _number(payload.get('delta'))))
