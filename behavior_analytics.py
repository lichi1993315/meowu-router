"""Bounded player behavior projections; raw facts are immutable evidence.

Intake projects one action and marks a dirty key. Importer replays <=8 players / <=20000 relevant
points per player; oversized histories are withheld rather than truncated.
"""
import json
import logging
import math
import sqlite3
import time
from bisect import bisect_right
from datetime import datetime, timezone
from journey_analytics import stamp, iso
from sqlite_runtime import mark_schema, transaction, TransactionBudgetExceeded, is_busy

VERSION = 'portrait-v1'
STATES = ('active', 'viewing', 'waiting', 'idle', 'unknown', 'background')
CATEGORIES = ('ai', 'cat', 'fishing', 'farming', 'restaurant', 'other')
METRICS = STATES + CATEGORIES
# Names are protocol identifiers; UI labels are explicit, never generated type names.
ACTIONS = {
    'ai_building_generation': ('ai', 'AI 建筑生成'), 'ai_adventure_action': ('ai', 'AI 冒险操作'),
    'cat_conversation': ('ai', '向猫说话'),
    'player_typing_started': ('ai', '开始输入'), 'player_typing_finished': ('ai', '结束输入'),
    'player_cat_interaction': ('cat', '与猫互动'),
    'theater_choice': ('ai', '小剧场选择'), 'theater_view': ('ai', '小剧场展示'),
    'theater_exit': ('ai', '退出小剧场'),
    'fishing_started': ('fishing', '开始钓鱼'), 'fishing_finished': ('fishing', '结束钓鱼'),
    'fishing_catch': ('fishing', '钓获鱼'),
    'farming_till': ('farming', '开垦'), 'farming_plant': ('farming', '播种'),
    'farming_water': ('farming', '浇水'), 'farming_harvest': ('farming', '收获'),
    'restaurant_order_taken': ('restaurant', '接单'), 'restaurant_served': ('restaurant', '上菜'),
    'cooking_complete': ('restaurant', '做菜完成'), 'cooking_completed': ('restaurant', '做菜完成'),
    'building_place': ('other', '放置建筑'), 'building_placed': ('other', '放置建筑'),
    'shop_purchase': ('other', '购买'), 'player_skin_changed': ('other', '换装'),
}
JOURNEY_KINDS = ('journey_started','journey_context','journey_world_ready','journey_checkpoint',
                 'journey_node_started','journey_node_ended','journey_page','journey_wait',
                 'journey_business','journey_leave','journey_quit','journey_foreground','journey_background')
RELEVANT_KINDS = frozenset(ACTIONS) | frozenset(JOURNEY_KINDS)
KINDS_SQL = ','.join("'" + k + "'" for k in sorted(RELEVANT_KINDS))

# Replays do not deserialize chat bodies, typing context or raw metadata. These
# remain in facts for the paged timeline; projection strings are <=256 characters.
_TEXT_FIELDS = ('server_session_id','current_node','node_id','status','source','speaker','surface','input_source',
                'trigger_source','choice_class','item_name','crop_name','pet_name','cat_name','outcome','reason',
                'operation_id','input_id','theater_event_id','flow_id','phase')
_TEXT_SQL = ','.join("'"+k+"',CASE WHEN json_type(f.payload_json,'$."+k+"')='text' THEN substr(json_extract(f.payload_json,'$."+k+"'),1,256) END" for k in _TEXT_FIELDS)
_COUNTER_SQL = ','.join("'"+k+"',CASE WHEN json_type(f.payload_json,'$.participation."+k+"') IN ('integer','real') THEN json_extract(f.payload_json,'$.participation."+k+"') END" for k in METRICS+('monotonic_seconds',))
_SNAPSHOT_SQL = "CASE WHEN json_type(f.payload_json,'$.participation')='object' THEN json_object("+_COUNTER_SQL+",'version',substr(json_extract(f.payload_json,'$.participation.version'),1,32),'in_world',json_extract(f.payload_json,'$.participation.in_world')) END"
PROJECTION_SQL = """f.event_id,f.user_id,f.session_id,f.client_platform,f.release_version,f.is_development_build,
 f.occurred_at,f.event_type,f.actor_is_player,f.sequence,json_object("""+_TEXT_SQL+",'participation',"+_SNAPSHOT_SQL+",'fish',json_object('fish_name',substr(json_extract(f.payload_json,'$.fish.fish_name'),1,256))) payload_json"


def ensure_behavior_schema(conn):
    metrics = ','.join(m + ' REAL' for m in METRICS)
    schema = '''CREATE TABLE IF NOT EXISTS behavior_dirty(user_id TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS behavior_projection_status(user_id TEXT PRIMARY KEY,status TEXT,point_count INTEGER,updated_at TEXT);
      CREATE TABLE IF NOT EXISTS behavior_actions(event_id TEXT PRIMARY KEY,user_id TEXT,session_id TEXT,
        occurred_at TEXT,sequence INTEGER,category TEXT,label TEXT,event_type TEXT,source TEXT,outcome TEXT,object_name TEXT,operation_id TEXT,initiated INTEGER,covered INTEGER);
      CREATE INDEX IF NOT EXISTS idx_behavior_actions_user_time ON behavior_actions(user_id,occurred_at,sequence);
      CREATE TABLE IF NOT EXISTS behavior_intervals(event_id TEXT PRIMARY KEY,user_id TEXT,run_id TEXT,
        session_id TEXT,start_at TEXT,end_at TEXT,quality TEXT,''' + metrics + ''');
      CREATE INDEX IF NOT EXISTS idx_behavior_intervals_user_time ON behavior_intervals(user_id,start_at,end_at);
      CREATE TABLE IF NOT EXISTS behavior_players(user_id TEXT PRIMARY KEY,first_at TEXT,session_id TEXT,
        client_platform TEXT,release_version TEXT,is_development_build INTEGER,cohort_kind TEXT,rule_version TEXT,
        quality TEXT,preference TEXT,participation_style TEXT,''' + metrics + ''');
      CREATE INDEX IF NOT EXISTS idx_behavior_players_first ON behavior_players(first_at);
      CREATE TABLE IF NOT EXISTS behavior_opening(user_id TEXT,window_minutes INTEGER,category TEXT,
        action_count INTEGER,first_at TEXT,PRIMARY KEY(user_id,window_minutes,category));
      CREATE TABLE IF NOT EXISTS behavior_quality_points(event_id TEXT PRIMARY KEY,user_id TEXT,occurred_at TEXT,reason TEXT);
      CREATE INDEX IF NOT EXISTS idx_behavior_quality_points ON behavior_quality_points(user_id,occurred_at);
      CREATE TABLE IF NOT EXISTS behavior_migrations(name TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS behavior_backfill_progress(name TEXT PRIMARY KEY,last_user_id TEXT NOT NULL);
    '''
    for statement in schema.split(';'):
        if statement.strip(): conn.execute(statement)
    columns = {r[1] for r in conn.execute('PRAGMA table_info(behavior_actions)')}
    for column in ('initiated', 'covered'):
        if column not in columns: conn.execute('ALTER TABLE behavior_actions ADD COLUMN '+column+' INTEGER')
    dirty_columns={r[1] for r in conn.execute('PRAGMA table_info(behavior_dirty)')}
    for name, definition in (('revision','INTEGER NOT NULL DEFAULT 1'),
                             ('pending','INTEGER NOT NULL DEFAULT 1'),
                             ('retry_after','REAL NOT NULL DEFAULT 0')):
        if name not in dirty_columns:
            conn.execute('ALTER TABLE behavior_dirty ADD COLUMN '+name+' '+definition)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_behavior_dirty_pending ON behavior_dirty(pending,retry_after)')
    # Fresh databases have no history to queue. Existing players are queued only
    # by the explicit maintenance command in bounded, resumable batches.
    if not conn.execute('SELECT 1 FROM analytics_event_facts LIMIT 1').fetchone():
        conn.execute('INSERT OR IGNORE INTO behavior_migrations VALUES (?)', (VERSION,))
    if conn.execute('SELECT 1 FROM behavior_migrations WHERE name=?', (VERSION,)).fetchone():
        mark_schema(conn, 'behavior')


def backfill_existing_behavior(conn, batch=500):
    """Queue at most one user page; caller commits each page during maintenance."""
    if conn.execute('SELECT 1 FROM behavior_migrations WHERE name=?', (VERSION,)).fetchone():
        mark_schema(conn, 'behavior')
        return 0, True
    row=conn.execute('SELECT last_user_id FROM behavior_backfill_progress WHERE name=?',(VERSION,)).fetchone()
    after=row[0] if row else ''
    users=[r[0] for r in conn.execute('SELECT DISTINCT user_id FROM analytics_event_facts WHERE user_id>? ORDER BY user_id LIMIT ?', (after,batch))]
    conn.executemany('INSERT OR IGNORE INTO behavior_dirty(user_id) VALUES (?)',((u,) for u in users))
    if users:
        conn.execute('INSERT OR REPLACE INTO behavior_backfill_progress VALUES (?,?)',(VERSION,users[-1]))
    done=len(users)<batch
    if done:
        conn.execute('INSERT OR IGNORE INTO behavior_migrations VALUES (?)',(VERSION,))
        mark_schema(conn, 'behavior')
    return len(users),done


def mark_behavior_dirty(conn, user, event_type=None):
    if event_type is not None and event_type not in RELEVANT_KINDS: return
    conn.execute('''INSERT INTO behavior_dirty(user_id) VALUES (?) ON CONFLICT(user_id) DO UPDATE SET
        revision=revision+1,pending=1,retry_after=0''',(user,))


def classify_action(e):
    kind = e['event_type']; p = e['p']
    if kind not in ACTIONS or not e.get('actor_is_player'):
        return None
    if p.get('source') in ('automatic', 'cat', 'system'):
        return None
    if kind == 'cat_conversation' and p.get('speaker') != 'player':
        return None
    category, label = ACTIONS[kind]
    if kind.startswith('player_typing') and p.get('surface') not in ('cat_chat', 'theater'):
        category = 'other'
    source = p.get('input_source') or p.get('trigger_source') or 'unknown'
    if kind == 'theater_view': source = 'exposure'
    if kind == 'theater_choice' and p.get('choice_class') == 'decline': source = 'declined'
    # No inference of free text versus preset from player_to_cat_private/public.
    if kind == 'cat_conversation' and source == 'unknown': source = 'player_unspecified'
    fish = p.get('fish') if isinstance(p.get('fish'), dict) else {}
    obj = p.get('item_name') or p.get('crop_name') or p.get('pet_name') or p.get('cat_name') or fish.get('fish_name')
    def text(value): return value if isinstance(value, str) else None
    return dict(event_id=e['event_id'], user_id=e['user_id'], session_id=e['session_id'],
                occurred_at=e.get('occurred_at'), sequence=e.get('sequence'), category=category,
                label=label, event_type=kind, source=text(source) or 'unknown', outcome=text(p.get('outcome') or p.get('phase') or p.get('reason')),
                object_name=text(obj), operation_id=text(p.get('operation_id') or p.get('input_id') or p.get('theater_event_id') or p.get('flow_id')),
                initiated=int(kind not in ('player_typing_started','player_typing_finished','fishing_finished','fishing_catch','theater_view','theater_exit')
                              and source not in ('exposure','declined') and (kind!='ai_building_generation' or p.get('phase')=='requested')), covered=0)



def upsert_behavior_action(conn, identity, user, session, event):
    """Project one human-readable action on intake; timeline freshness does not wait for cohort replay."""
    kind = event.get('event_type', '')
    if kind not in ACTIONS: return
    payload = event.get('payload')
    actor = event.get('actor') or {}
    row = classify_action(dict(event_id=identity, user_id=user, session_id=session, event_type=kind,
        occurred_at=event.get('event_real_time_iso'), sequence=event.get('sequence'),
        actor_is_player=actor.get('is_player') is True, p=payload if isinstance(payload,dict) else {}))
    if row is None:
        conn.execute('DELETE FROM behavior_actions WHERE event_id=?', (identity,))
        return
    keys = tuple(row)
    assignments = ','.join(k+'=excluded.'+k for k in keys if k not in ('event_id','covered'))
    conn.execute('INSERT INTO behavior_actions ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+
        ') ON CONFLICT(event_id) DO UPDATE SET '+assignments, tuple(row[k] for k in keys))


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def profile(totals, quality):
    """Versioned, interpretable early-window rules; no future behavior is consulted."""
    preference = style = 'insufficient'
    active = totals['active']; observed = sum(totals[m] for m in STATES)
    if quality == 'observed' and active >= 300:
        ai = totals['ai']; sim = sum(totals[m] for m in ('fishing', 'farming', 'restaurant'))
        if ai / active >= .6: preference = 'ai'
        elif sim / active >= .6: preference = 'simulation'
        elif ai > 0 and sim > 0 and (ai + sim) / active >= .6: preference = 'mixed'
        else: preference = 'other'
    if quality == 'observed' and observed >= 1200:
        if (totals['idle'] + totals['background']) / observed >= .7: style = 'idle'
        elif active / observed >= .6: style = 'active'
        else: style = 'intermittent'
    return preference, style


def reduce_player(events):
    actions = [a for e in events if (a := classify_action(e))]
    points = [e for e in events if e['event_type'].startswith('journey_') and e.get('t') is not None]
    worlds = [e for e in points if e['event_type'] == 'journey_world_ready']
    if not worlds: return actions, [], None, [], []
    first = min(worlds, key=lambda e: (e['t'], e.get('sequence') or 0)); start = first['t']
    totals = dict.fromkeys(METRICS, 0.0); intervals = []; quality_points = []; previous = {}; early_quality = 'observed'
    # Sequence is authoritative within a run; UTC drift becomes a gap, never negative playtime.
    points.sort(key=lambda e: (e['session_id'], e.get('sequence') or 0, e['event_id']))
    for e in points:
        run = e['session_id']; prev = previous.get(run); previous[run] = e
        if (not prev or prev['event_type'] in ('journey_leave', 'journey_quit')
                or not prev['p'].get('participation', {}).get('in_world', prev['p'].get('current_node') == 'world')):
            continue
        a = prev['p'].get('participation', {}); b = e['p'].get('participation', {})
        dt = e['t'] - prev['t']; delta = {}
        valid = a.get('version') == b.get('version') == 'participation-v1'
        for m in METRICS:
            x, y = a.get(m), b.get(m)
            if not finite(x) or not finite(y) or y < x: valid = False
            delta[m] = y - x if finite(x) and finite(y) else None
        mono_a, mono_b = a.get('monotonic_seconds'), b.get('monotonic_seconds')
        valid = valid and finite(mono_a) and finite(mono_b)
        if valid:
            md = mono_b - mono_a
            valid = (0 <= dt <= 90 and 0 <= md <= 90 and abs(dt-md) <= 2
                     and abs(sum(delta[m] for m in CATEGORIES)-delta['active']) < .05
                     and sum(delta[m] for m in STATES) <= md+.1
                     and sum(delta[m] for m in STATES) >= md-2)
        if dt == 0 and valid: continue
        intervals.append(dict(event_id=e['event_id'], user_id=first['user_id'], run_id=run,
            session_id=prev['p'].get('server_session_id', ''), start_at=iso(prev['t']), end_at=iso(e['t']),
            quality='observed' if valid else 'gap', **{m: delta[m] if valid else None for m in METRICS}))
        if not valid:
            quality_points.append(dict(event_id=e['event_id'],user_id=first['user_id'],occurred_at=iso(prev['t']),reason='invalid_interval'))
    # Conservatively withhold both overlapping runs. Never pick a favorable client or sum them.
    intervals.sort(key=lambda i: (stamp(i['start_at']), stamp(i['end_at'])))
    frontier = None
    for iv in intervals:
        if frontier and stamp(iv['start_at']) < stamp(frontier['end_at']) - .001:
            frontier['quality'] = iv['quality'] = 'concurrent'
        if frontier is None or stamp(iv['end_at']) > stamp(frontier['end_at']): frontier = iv
    for iv in intervals:
        lo, hi = stamp(iv['start_at']), stamp(iv['end_at'])
        overlap = max(0, min(hi, start+86400)-max(lo,start))
        if hi < lo and lo < start+86400: early_quality = 'incomplete'
        if overlap <= 0: continue
        if iv['quality'] != 'observed': early_quality = 'incomplete'; continue
        for m in METRICS: totals[m] += iv[m] * overlap/(hi-lo)
    coverage = {}
    for iv in intervals:
        for key in {iv['run_id'], iv['session_id']}:
            coverage.setdefault(key, []).append((stamp(iv['start_at']), stamp(iv['end_at']), iv['quality']))
    for key in coverage: coverage[key].sort()
    starts = {key: [r[0] for r in ranges] for key, ranges in coverage.items()}
    for a in actions:
        at = stamp(a['occurred_at'])
        if at is None or at < start: continue
        ranges = coverage.get(a['session_id'], [])
        index = bisect_right(starts.get(a['session_id'], []), at) - 1
        if index >= 0 and ranges[index][1] >= at:
            a['covered'] = int(ranges[index][2]=='observed')
        if not a['covered']:
            quality_points.append(dict(event_id=a['event_id'],user_id=first['user_id'],occurred_at=a['occurred_at'],reason='action_without_clock'))
            if at < start+86400: early_quality = 'incomplete'
    for last in previous.values():
        if (last['p'].get('participation', {}).get('in_world') and last['event_type'] not in ('journey_leave', 'journey_quit')):
            quality_points.append(dict(event_id=last['event_id'],user_id=first['user_id'],occurred_at=iso(last['t']),reason='missing_tail'))
            if start <= last['t'] < start+86400: early_quality = 'incomplete'
    if first['p'].get('participation', {}).get('version') != 'participation-v1': early_quality = 'missing_early'
    if any(a['occurred_at'] is None or (stamp(a['occurred_at']) is not None and stamp(a['occurred_at']) < start-60) for a in actions):
        early_quality = 'missing_early'
    first_test = bool(first['is_development_build']) or first['client_platform'] == 'editor'
    if any(start <= e['t'] < start+86400 and (bool(e['is_development_build']) or e['client_platform']=='editor') != first_test for e in points):
        early_quality = 'mixed_test_scope'
    created = any(e['event_type'] == 'journey_node_ended' and e['p'].get('node_id') == 'player_create'
                  and e['p'].get('status') == 'completed' and e['t'] <= start for e in points)
    preference, style = profile(totals, early_quality)
    player = dict(user_id=first['user_id'], first_at=iso(start), session_id=first['p'].get('server_session_id',''),
                  client_platform=first['client_platform'], release_version=first['release_version'],
                  is_development_build=first['is_development_build'], cohort_kind='new_observed' if created else 'existing_or_unknown',
                  rule_version=VERSION, quality=early_quality, preference=preference, participation_style=style, **totals)
    opening = {}
    for a in actions:
        at = stamp(a['occurred_at'])
        if at is None or at < start or not a['initiated']: continue
        for minutes in (5, 15, 30):
            if at >= start + minutes*60: continue
            key = (minutes, a['category']); row = opening.setdefault(key, [first['user_id'], minutes, a['category'], 0, a['occurred_at']])
            row[3] += 1
            if stamp(row[4]) > at: row[4] = a['occurred_at']
    return actions, intervals, player, list(opening.values()), quality_points


def refresh_behavior(conn, limit=8, max_points=20000):
    """Compute outside the writer lease and publish only unchanged revisions."""
    if conn.in_transaction:
        raise RuntimeError('behavior refresh requires a committed connection')
    users = conn.execute('''SELECT user_id,revision FROM behavior_dirty
        WHERE pending=1 AND retry_after<=? ORDER BY rowid LIMIT ?''',(time.time(),limit)).fetchall()
    now = iso(datetime.now(timezone.utc).timestamp())
    started = time.monotonic()
    processed = 0
    for index, (user, revision) in enumerate(users):
        if index and time.monotonic()-started >= 2: break
        # Index user/kind/time bounds work before JSON decoding. Old un-timed actions remain raw evidence.
        rows = conn.execute(f'''SELECT {PROJECTION_SQL} FROM analytics_event_facts f WHERE f.user_id=?
          AND f.event_type IN ({KINDS_SQL})
          UNION ALL SELECT {PROJECTION_SQL} FROM journey_run_owners o JOIN analytics_event_facts f ON f.session_id=o.run_id
          WHERE o.user_id=? AND f.user_id GLOB 'anon:*' AND f.user_id!=? AND f.event_type IN ({KINDS_SQL})
          LIMIT ?''', (user, user, user, max_points+1))
        columns = [c[0] for c in rows.description]; events = [dict(zip(columns, r)) for r in rows]
        status = 'ready'
        prepared = {}
        if len(events) > max_points: status = 'history_limit'
        elif user.startswith('anon:'): status = 'anonymous'
        else:
            for e in events:
                e['p'] = json.loads(e['payload_json']); e['t'] = stamp(e['occurred_at'])
                if not isinstance(e['p'], dict): e['p'] = {}
                if not isinstance(e['p'].get('participation'), dict): e['p']['participation'] = {}
                e['user_id'] = user
            actions, intervals, player, opening, quality_points = reduce_player(events)
            for table, records in (('behavior_actions', actions), ('behavior_intervals', intervals), ('behavior_players', [player] if player else []), ('behavior_quality_points', quality_points)):
                if records:
                    keys = tuple(records[0])
                    prepared[table] = (keys, [tuple(r[k] for k in keys) for r in records])
            prepared['behavior_opening'] = (None, opening)
            if not player: status = 'missing_world_entry'
        try:
            with transaction(conn, 'behavior.publish', budget=1):
                current=conn.execute('SELECT revision,pending FROM behavior_dirty WHERE user_id=?',(user,)).fetchone()
                if current != (revision,1):
                    continue
                for table in ('behavior_intervals', 'behavior_players', 'behavior_opening', 'behavior_quality_points'):
                    conn.execute('DELETE FROM '+table+' WHERE user_id=?',(user,))
                for table, (keys, records) in prepared.items():
                    if keys:
                        conn.executemany('INSERT OR REPLACE INTO '+table+' ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',records)
                    else:
                        conn.executemany('INSERT INTO '+table+' VALUES (?,?,?,?,?)',records)
                conn.execute('INSERT OR REPLACE INTO behavior_projection_status VALUES (?,?,?,?)',(user,status,len(events),now))
                conn.execute('UPDATE behavior_dirty SET pending=0,retry_after=0 WHERE user_id=? AND revision=?',(user,revision))
            processed += 1
        except (sqlite3.OperationalError, TransactionBudgetExceeded) as error:
            if not is_busy(error) and not isinstance(error,TransactionBudgetExceeded):
                raise
            logging.getLogger(__name__).warning('Behavior remains pending user=%s reason=%s',user,type(error).__name__)
            try:
                with transaction(conn, 'behavior.defer', budget=1):
                    conn.execute('UPDATE behavior_dirty SET retry_after=? WHERE user_id=? AND revision=?',(time.time()+300,user,revision))
            except sqlite3.OperationalError as defer_error:
                if not is_busy(defer_error): raise
    return processed
