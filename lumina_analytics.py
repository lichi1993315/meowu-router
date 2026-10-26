"""Lumina PawFishing (web) playtest events: raw rows plus a per-session rollup and step table.

Kept apart from the Unity facts (analytics_event_facts, journey_*): the web game has its own
identities and flow, and must not move the Unity reports. Client contract and metric definitions:
lumina_pawfishing docs/ANALYTICS.md. ACK only follows commit; a retried event_id is a no-op.
"""
import json
import re
from datetime import datetime, timezone

from sqlite_runtime import connection, transaction, mark_schema, require_schema
from telemetry_time import parse_timestamp

MAX_EVENTS = 100
MAX_BYTES = 262144
TYPE = re.compile(r'^[a-z][a-z0-9_]{0,39}$')
# A session ends this long after its last event (docs/ANALYTICS.md 2.3).
END_AFTER_S = 300

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lumina_events (
    event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, player_id TEXT NOT NULL, archive_id TEXT,
    seq INTEGER NOT NULL, type TEXT NOT NULL, occurred_at TEXT NOT NULL, received_at TEXT NOT NULL,
    mono_ms INTEGER, eff_s REAL, fg_s REAL, afk_s REAL, bg_s REAL,
    build TEXT, platform TEXT, tb TEXT, src TEXT, internal INTEGER NOT NULL DEFAULT 0, session_n INTEGER,
    task TEXT, step TEXT, room TEXT, day INTEGER, stage TEXT, data_json TEXT NOT NULL, schema_version INTEGER);
CREATE INDEX IF NOT EXISTS idx_lumina_events_session ON lumina_events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_lumina_events_type_time ON lumina_events(type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_lumina_events_player ON lumina_events(player_id, occurred_at);

CREATE TABLE IF NOT EXISTS lumina_sessions (
    session_id TEXT PRIMARY KEY, player_id TEXT NOT NULL, archive_id TEXT, session_n INTEGER,
    tb TEXT, src TEXT, build TEXT, platform TEXT, internal INTEGER NOT NULL DEFAULT 0, fresh INTEGER NOT NULL DEFAULT 0,
    first_at TEXT NOT NULL, last_at TEXT NOT NULL, events INTEGER NOT NULL DEFAULT 0, last_seq INTEGER NOT NULL DEFAULT 0,
    ready_at TEXT, load_ms INTEGER, page_ms INTEGER, load_failed INTEGER NOT NULL DEFAULT 0,
    stage TEXT, stage_seq INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL DEFAULT 0, chain_done INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0, left_page INTEGER NOT NULL DEFAULT 0,
    max_eff_s REAL NOT NULL DEFAULT 0, max_fg_s REAL NOT NULL DEFAULT 0, max_afk_s REAL NOT NULL DEFAULT 0, max_bg_s REAL NOT NULL DEFAULT 0,
    last_task TEXT, last_step TEXT, last_room TEXT, last_day INTEGER,
    act_type TEXT, act_seq INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_lumina_sessions_player ON lumina_sessions(player_id, first_at);
CREATE INDEX IF NOT EXISTS idx_lumina_sessions_time ON lumina_sessions(first_at);

CREATE TABLE IF NOT EXISTS lumina_steps (
    session_id TEXT NOT NULL, step TEXT NOT NULL, task TEXT, task_i INTEGER, step_i INTEGER, label TEXT,
    started_eff REAL, done_eff REAL, dur_eff REAL, resumed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, step));
CREATE INDEX IF NOT EXISTS idx_lumina_steps_step ON lumina_steps(task_i, step_i);

DROP VIEW IF EXISTS lumina_sessions_v;
CREATE VIEW lumina_sessions_v AS
SELECT s.*,
    CASE WHEN (julianday('now') - julianday(s.last_at)) * 86400 > %(end)d THEN 1 ELSE 0 END AS ended,
    CASE WHEN s.ready_at IS NULL THEN 0 WHEN s.max_eff_s < 30 THEN 1 WHEN s.max_eff_s < 60 THEN 2
         WHEN s.max_eff_s < 180 THEN 3 WHEN s.max_eff_s < 300 THEN 4 WHEN s.max_eff_s < 600 THEN 5
         WHEN s.max_eff_s < 1200 THEN 6 WHEN s.max_eff_s < 1800 THEN 7 WHEN s.max_eff_s < 3600 THEN 8 ELSE 9 END AS bucket_i,
    CASE WHEN s.ready_at IS NULL THEN '加载中' WHEN s.max_eff_s < 30 THEN '0–30s' WHEN s.max_eff_s < 60 THEN '30s–1m'
         WHEN s.max_eff_s < 180 THEN '1–3m' WHEN s.max_eff_s < 300 THEN '3–5m' WHEN s.max_eff_s < 600 THEN '5–10m'
         WHEN s.max_eff_s < 1200 THEN '10–20m' WHEN s.max_eff_s < 1800 THEN '20–30m' WHEN s.max_eff_s < 3600 THEN '30–60m' ELSE '60m+' END AS bucket
FROM lumina_sessions s;
""" % {'end': END_AFTER_S}

# Envelope fields; `where` is flattened into task/step/room/day/stage.
_EVENT_COLS = ('event_id', 'session_id', 'player_id', 'archive_id', 'seq', 'type', 'occurred_at', 'received_at',
               'mono_ms', 'eff_s', 'fg_s', 'afk_s', 'bg_s', 'build', 'platform', 'tb', 'src', 'internal', 'session_n',
               'task', 'step', 'room', 'day', 'stage', 'data_json', 'schema_version')
# Types that are the page's own pulse, not something the player did (the session's last act skips them).
_PULSE = {'heartbeat', 'visibility', 'leave', 'load_stage'}


def ensure_lumina_schema(conn):
    conn.executescript(SCHEMA_SQL)
    mark_schema(conn, 'lumina')


def _str(value, limit, *, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError('invalid string field')
    return value


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _row(event, received):
    if not isinstance(event, dict):
        raise ValueError('event must be an object')
    eid = _str(event.get('event_id'), 128)
    session = _str(event.get('session_id'), 64)
    if not eid.startswith(session + ':'):
        raise ValueError('event id must belong to its session')
    kind = event.get('type')
    if not isinstance(kind, str) or not TYPE.match(kind):
        raise ValueError('invalid event type')
    seq = event.get('seq')
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise ValueError('invalid sequence')
    stamp = parse_timestamp(_str(event.get('t'), 40))
    if stamp.tzinfo is None:
        raise ValueError('timestamp timezone required')
    where = event.get('where') if isinstance(event.get('where'), dict) else {}
    data = event.get('data') if isinstance(event.get('data'), dict) else {}
    text = lambda v, n=64: v[:n] if isinstance(v, str) else None
    day = where.get('day')
    return {
        'event_id': eid, 'session_id': session, 'player_id': _str(event.get('player_id'), 64),
        'archive_id': text(event.get('archive_id')), 'seq': seq, 'type': kind,
        'occurred_at': stamp.astimezone(timezone.utc).isoformat(), 'received_at': received,
        'mono_ms': int(_num(event.get('mono_ms')) or 0), 'eff_s': _num(event.get('eff_s')) or 0, 'fg_s': _num(event.get('fg_s')) or 0,
        'afk_s': _num(event.get('afk_s')) or 0, 'bg_s': _num(event.get('bg_s')) or 0,
        'build': text(event.get('build'), 40), 'platform': text(event.get('platform'), 16), 'tb': text(event.get('tb')), 'src': text(event.get('src')),
        'internal': int(event.get('internal') is True), 'session_n': event.get('session_n') if isinstance(event.get('session_n'), int) else None,
        'task': text(where.get('task')), 'step': text(where.get('step')), 'room': text(where.get('room')),
        'day': day if isinstance(day, int) and not isinstance(day, bool) else None, 'stage': text(where.get('stage'), 80),
        'data_json': json.dumps(data, ensure_ascii=False)[:8192], 'schema_version': event.get('v') if isinstance(event.get('v'), int) else None,
        '_data': data,
    }


def _rollup(conn, r):
    """Fold one new event into its session (any order: the latest seq wins the place fields)."""
    data, kind = r['_data'], r['type']
    is_act = kind not in _PULSE
    conn.execute("""INSERT INTO lumina_sessions (session_id, player_id, archive_id, session_n, tb, src, build, platform, internal, fresh,
            first_at, last_at, events, last_seq, ready_at, load_ms, page_ms, load_failed, stage, stage_seq, created, chain_done, errors, left_page,
            max_eff_s, max_fg_s, max_afk_s, max_bg_s, last_task, last_step, last_room, last_day, act_type, act_seq)
        VALUES (:session_id, :player_id, :archive_id, :session_n, :tb, :src, :build, :platform, :internal, :fresh,
            :occurred_at, :occurred_at, 1, :seq, :ready_at, :load_ms, :page_ms, :load_failed, :stage, :stage_seq, :created, :chain_done, :errors, :left_page,
            :eff_s, :fg_s, :afk_s, :bg_s, :task, :step, :room, :day, :act_type, :act_seq)
        ON CONFLICT(session_id) DO UPDATE SET
            archive_id = COALESCE(excluded.archive_id, archive_id),
            session_n = COALESCE(session_n, excluded.session_n), tb = COALESCE(tb, excluded.tb), src = COALESCE(src, excluded.src),
            build = COALESCE(build, excluded.build), platform = COALESCE(platform, excluded.platform),
            internal = MAX(internal, excluded.internal), fresh = MAX(fresh, excluded.fresh),
            first_at = MIN(first_at, excluded.first_at), last_at = MAX(last_at, excluded.last_at), events = events + 1,
            ready_at = COALESCE(ready_at, excluded.ready_at), load_ms = COALESCE(load_ms, excluded.load_ms), page_ms = COALESCE(page_ms, excluded.page_ms),
            load_failed = MAX(load_failed, excluded.load_failed),
            stage = CASE WHEN excluded.stage_seq > stage_seq THEN excluded.stage ELSE stage END, stage_seq = MAX(stage_seq, excluded.stage_seq),
            created = MAX(created, excluded.created), chain_done = MAX(chain_done, excluded.chain_done),
            errors = errors + excluded.errors, left_page = MAX(left_page, excluded.left_page),
            max_eff_s = MAX(max_eff_s, excluded.max_eff_s), max_fg_s = MAX(max_fg_s, excluded.max_fg_s),
            max_afk_s = MAX(max_afk_s, excluded.max_afk_s), max_bg_s = MAX(max_bg_s, excluded.max_bg_s),
            last_task = CASE WHEN excluded.last_seq > last_seq THEN excluded.last_task ELSE last_task END,
            last_step = CASE WHEN excluded.last_seq > last_seq THEN excluded.last_step ELSE last_step END,
            last_room = CASE WHEN excluded.last_seq > last_seq THEN excluded.last_room ELSE last_room END,
            last_day = CASE WHEN excluded.last_seq > last_seq THEN excluded.last_day ELSE last_day END,
            last_seq = MAX(last_seq, excluded.last_seq),
            act_type = CASE WHEN excluded.act_seq > act_seq THEN excluded.act_type ELSE act_type END, act_seq = MAX(act_seq, excluded.act_seq)""",
        {**{k: v for k, v in r.items() if k != '_data'},
         'fresh': int(kind == 'boot' and data.get('fresh') is True),
         'ready_at': r['occurred_at'] if kind == 'ready' else None,
         'load_ms': int(data['load_ms']) if kind == 'ready' and _num(data.get('load_ms')) is not None else None,
         'page_ms': int(data['page_ms']) if kind == 'ready' and _num(data.get('page_ms')) is not None else None,
         'load_failed': int(kind == 'load_fail'), 'stage_seq': r['seq'] if r['stage'] else 0,
         'created': int(kind == 'create_done'), 'chain_done': int(kind == 'chain_done'), 'errors': int(kind in ('error', 'webgl_lost')),
         'left_page': int(kind == 'leave'), 'act_type': kind if is_act else None, 'act_seq': r['seq'] if is_act else 0})
    if kind == 'step_start' and isinstance(data.get('step'), str):
        conn.execute("""INSERT INTO lumina_steps (session_id, step, task, task_i, step_i, label, started_eff, resumed) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(session_id, step) DO UPDATE SET started_eff = COALESCE(started_eff, excluded.started_eff), label = COALESCE(label, excluded.label),
                resumed = MAX(resumed, excluded.resumed)""",
            (r['session_id'], data['step'][:64], str(data.get('task'))[:64], data.get('task_i'), data.get('step_i'),
             data['label'][:80] if isinstance(data.get('label'), str) else None, r['eff_s'], int(data.get('resumed') is True)))
    elif kind == 'step_done' and isinstance(data.get('step'), str):
        conn.execute("""INSERT INTO lumina_steps (session_id, step, task, task_i, step_i, done_eff, dur_eff, resumed) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(session_id, step) DO UPDATE SET done_eff = excluded.done_eff, dur_eff = excluded.dur_eff, resumed = MAX(resumed, excluded.resumed)""",
            (r['session_id'], data['step'][:64], str(data.get('task'))[:64], data.get('task_i'), data.get('step_i'), r['eff_s'],
             _num(data.get('eff')), int(data.get('resumed') is True)))


def accept_lumina_batch(db_path, payload):
    """Validate the whole batch, then write it in one transaction; retried events are skipped."""
    events = payload.get('events')
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
        raise ValueError('events must contain 1..%d records' % MAX_EVENTS)
    received = datetime.now(timezone.utc).isoformat()
    rows = [_row(e, received) for e in events]
    accepted = []
    with connection(db_path) as conn, transaction(conn, 'lumina.batch'):
        require_schema(conn, 'lumina')
        for r in rows:
            owner = conn.execute('SELECT session_id, player_id FROM lumina_events WHERE event_id=?', (r['event_id'],)).fetchone()
            if owner and owner != (r['session_id'], r['player_id']):
                raise ValueError('event id belongs to a different player')
        for r in rows:
            cursor = conn.execute('INSERT OR IGNORE INTO lumina_events (%s) VALUES (%s)' % (','.join(_EVENT_COLS), ','.join('?' * len(_EVENT_COLS))),
                                  tuple(r[c] for c in _EVENT_COLS))
            if cursor.rowcount:
                _rollup(conn, r)
            accepted.append(r['event_id'])
    return {'accepted_event_ids': accepted}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Create the lumina_* tables (maintenance only).')
    parser.add_argument('--db', default='/app/data/conversations.db')
    args = parser.parse_args()
    with connection(args.db) as c:
        ensure_lumina_schema(c)
        c.commit()
