from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from version_utils import release_version_from_client_version


HEARTBEAT_STALE_AFTER_SEC = 180
_SCHEMA_READY: set[str] = set()
_SCHEMA_LOCK = threading.Lock()


PLAYTIME_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS play_session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    received_at TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    player_session_id TEXT,
    player_id TEXT,
    client_version TEXT,
    release_version TEXT,
    country TEXT,
    client_sent_at TEXT,
    sequence INTEGER,
    game_duration_sec REAL,
    foreground_duration_sec REAL,
    active_duration_sec REAL,
    app_state TEXT,
    last_gameplay_event_at TEXT,
    outbox_id TEXT,
    payload_size_bytes INTEGER DEFAULT 0,
    payload_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_play_session_events_dedupe
    ON play_session_events(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_play_session_events_session
    ON play_session_events(user_id, session_id, received_at);
CREATE INDEX IF NOT EXISTS idx_play_session_events_player_session
    ON play_session_events(user_id, player_session_id);
CREATE INDEX IF NOT EXISTS idx_play_session_events_received
    ON play_session_events(received_at);
CREATE INDEX IF NOT EXISTS idx_play_session_events_release_version
    ON play_session_events(release_version);

CREATE TABLE IF NOT EXISTS play_session_rollups (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    player_session_id TEXT,
    player_id TEXT,
    client_version TEXT,
    release_version TEXT,
    country TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    login_at TEXT,
    logoff_at TEXT,
    last_client_sent_at TEXT,
    heartbeat_count INTEGER DEFAULT 0,
    event_count INTEGER DEFAULT 0,
    max_sequence INTEGER,
    logoff_duration_sec REAL,
    heartbeat_duration_sec REAL,
    server_span_sec REAL,
    estimated_tail_sec REAL DEFAULT 0,
    final_duration_sec REAL DEFAULT 0,
    duration_source TEXT NOT NULL DEFAULT 'none',
    status TEXT NOT NULL DEFAULT 'open',
    confidence TEXT NOT NULL DEFAULT 'low',
    end_reason TEXT,
    app_state TEXT,
    last_gameplay_event_at TEXT,
    last_event_type TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_play_session_rollups_user
    ON play_session_rollups(user_id);
CREATE INDEX IF NOT EXISTS idx_play_session_rollups_player_session
    ON play_session_rollups(user_id, player_session_id);
CREATE INDEX IF NOT EXISTS idx_play_session_rollups_last_seen
    ON play_session_rollups(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_play_session_rollups_duration_source
    ON play_session_rollups(duration_source);
CREATE INDEX IF NOT EXISTS idx_play_session_rollups_release_version
    ON play_session_rollups(release_version);
"""


PLAYTIME_VIEW_SQL = """
DROP VIEW IF EXISTS playtime_session_detail;
CREATE VIEW IF NOT EXISTS playtime_session_detail AS
SELECT
    r.*,
    CASE
        WHEN r.status = 'open'
             AND r.last_seen_at IS NOT NULL
             AND (julianday('now') - julianday(r.last_seen_at)) * 86400 > 180
        THEN 1
        ELSE 0
    END AS is_stale,
    ROUND(r.final_duration_sec / 60.0, 2) AS final_duration_min
FROM play_session_rollups r;

DROP VIEW IF EXISTS playtime_player_summary;
CREATE VIEW IF NOT EXISTS playtime_player_summary AS
SELECT
    r.user_id,
    COALESCE(
        NULLIF(us.nickname, ''),
        NULLIF(us.player_name, ''),
        r.user_id
    ) AS nickname,
    COUNT(*) AS session_count,
    SUM(CASE WHEN r.status = 'closed' THEN 1 ELSE 0 END) AS closed_session_count,
    SUM(CASE WHEN r.status = 'open' THEN 1 ELSE 0 END) AS open_session_count,
    SUM(CASE WHEN r.status = 'open' AND d.is_stale = 1 THEN 1 ELSE 0 END) AS stale_session_count,
    SUM(CASE WHEN r.duration_source = 'logoff' THEN 1 ELSE 0 END) AS logoff_session_count,
    SUM(CASE WHEN r.duration_source = 'heartbeat_client' THEN 1 ELSE 0 END) AS heartbeat_session_count,
    SUM(CASE WHEN r.confidence = 'low' THEN 1 ELSE 0 END) AS low_confidence_session_count,
    ROUND(SUM(COALESCE(r.final_duration_sec, 0)), 2) AS total_play_duration_sec,
    ROUND(AVG(COALESCE(r.final_duration_sec, 0)), 2) AS avg_session_duration_sec,
    MIN(r.first_seen_at) AS first_seen_at,
    MAX(r.last_seen_at) AS last_seen_at,
    GROUP_CONCAT(DISTINCT COALESCE(NULLIF(r.release_version, ''), '(empty)')) AS release_versions,
    GROUP_CONCAT(DISTINCT COALESCE(NULLIF(r.client_version, ''), '(empty)')) AS client_versions
FROM play_session_rollups r
JOIN playtime_session_detail d
    ON d.user_id = r.user_id AND d.session_id = r.session_id
LEFT JOIN user_sessions us
    ON us.user_id = r.user_id
GROUP BY r.user_id;
"""


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def ensure_playtime_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(PLAYTIME_SCHEMA_SQL)
    ensure_column(conn, "play_session_events", "release_version", "TEXT")
    ensure_column(conn, "play_session_events", "foreground_duration_sec", "REAL")
    ensure_column(conn, "play_session_events", "active_duration_sec", "REAL")
    ensure_column(conn, "play_session_events", "app_state", "TEXT")
    ensure_column(conn, "play_session_events", "last_gameplay_event_at", "TEXT")
    ensure_column(conn, "play_session_events", "payload_size_bytes", "INTEGER DEFAULT 0")
    ensure_column(conn, "play_session_events", "payload_json", "TEXT")
    ensure_column(conn, "play_session_rollups", "release_version", "TEXT")
    ensure_column(conn, "play_session_rollups", "estimated_tail_sec", "REAL DEFAULT 0")
    ensure_column(conn, "play_session_rollups", "last_event_type", "TEXT")
    conn.executescript(PLAYTIME_VIEW_SQL)


def ensure_playtime_schema_once(conn: sqlite3.Connection, db_path: str | Path) -> None:
    cache_key = str(db_path)
    with _SCHEMA_LOCK:
        if cache_key in _SCHEMA_READY:
            return
        ensure_playtime_schema(conn)
        conn.commit()
        _SCHEMA_READY.add(cache_key)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _header_value(headers: dict[str, Any], name: str) -> str:
    lower_name = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lower_name and value is not None:
            return str(value).strip()
    return ""


def _first_nonempty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _seconds_between(start: Any, end: Any) -> float:
    start_dt = _parse_iso(start)
    end_dt = _parse_iso(end)
    if not start_dt or not end_dt:
        return 0.0
    diff = (end_dt - start_dt).total_seconds()
    return max(0.0, diff)


def _session_meta(payload: dict[str, Any]) -> dict[str, Any]:
    telemetry = payload.get("gameplay_telemetry")
    if not isinstance(telemetry, dict):
        return {}
    meta = telemetry.get("session_meta")
    return meta if isinstance(meta, dict) else {}


def _duration_value(payload: dict[str, Any], meta: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _as_float(payload.get(key))
        if value is not None:
            return value
        value = _as_float(meta.get(key))
        if value is not None:
            return value
    return None


def _build_event(
    *,
    payload: dict[str, Any],
    headers: dict[str, Any],
    event_type: str,
    received_at: str,
    payload_size_bytes: int,
) -> dict[str, Any]:
    meta = _session_meta(payload)
    user_id = _first_nonempty(
        _header_value(headers, "x-user-id"),
        payload.get("user_id"),
        payload.get("player_id"),
        meta.get("user_id"),
        "unknown",
    )
    session_id = _first_nonempty(
        _header_value(headers, "x-session-id"),
        payload.get("session_id"),
        meta.get("session_id"),
    )
    if not session_id:
        session_id = f"unknown-{uuid.uuid4().hex[:8]}"

    player_id = _first_nonempty(
        _header_value(headers, "x-player-id"),
        payload.get("player_id"),
        payload.get("user_id"),
        user_id,
    )
    player_session_id = _first_nonempty(
        _header_value(headers, "x-player-session-id"),
        payload.get("player_session_id"),
        payload.get("playerSessionId"),
        meta.get("player_session_id"),
        meta.get("playerSessionId"),
    )
    client_version = _first_nonempty(
        _header_value(headers, "x-client-version"),
        payload.get("client_version"),
        meta.get("client_version"),
    )
    release_version = release_version_from_client_version(client_version)
    country = _first_nonempty(
        _header_value(headers, "cf-ipcountry"),
        payload.get("country"),
        payload.get("country_code"),
        meta.get("country"),
        meta.get("country_code"),
    )
    client_sent_at = _first_nonempty(
        payload.get("client_sent_at"),
        payload.get("timestamp"),
        payload.get("event_timestamp"),
        meta.get("client_sent_at"),
    )
    sequence = _as_int(
        _first_nonempty(
            payload.get("sequence"),
            payload.get("heartbeat_sequence"),
            payload.get("seq"),
        )
    )
    outbox_id = _first_nonempty(
        _header_value(headers, "x-outbox-id"),
        payload.get("outbox_id"),
        payload.get("outboxId"),
        (payload.get("outbox") or {}).get("id") if isinstance(payload.get("outbox"), dict) else "",
    )
    game_duration_sec = _duration_value(
        payload,
        meta,
        "game_duration_sec",
        "session_duration_sec",
        "play_duration_sec",
    )
    foreground_duration_sec = _duration_value(payload, meta, "foreground_duration_sec")
    active_duration_sec = _duration_value(payload, meta, "active_duration_sec")
    app_state = _first_nonempty(payload.get("app_state"), meta.get("app_state"))
    last_gameplay_event_at = _first_nonempty(
        payload.get("last_gameplay_event_at"),
        meta.get("last_gameplay_event_at"),
    )

    if outbox_id:
        dedupe_key = f"outbox:{event_type}:{user_id}:{session_id}:{outbox_id}"
    elif sequence is not None:
        dedupe_key = f"sequence:{event_type}:{user_id}:{session_id}:{sequence}"
    elif client_sent_at:
        dedupe_key = f"client_time:{event_type}:{user_id}:{session_id}:{client_sent_at}"
    else:
        dedupe_key = f"server_time:{event_type}:{user_id}:{session_id}:{received_at}"

    return {
        "dedupe_key": dedupe_key,
        "event_type": event_type,
        "received_at": received_at,
        "user_id": user_id,
        "session_id": session_id,
        "player_session_id": player_session_id,
        "player_id": player_id,
        "client_version": client_version,
        "release_version": release_version,
        "country": country,
        "client_sent_at": client_sent_at,
        "sequence": sequence,
        "game_duration_sec": game_duration_sec,
        "foreground_duration_sec": foreground_duration_sec,
        "active_duration_sec": active_duration_sec,
        "app_state": app_state,
        "last_gameplay_event_at": last_gameplay_event_at,
        "outbox_id": outbox_id,
        "payload_size_bytes": payload_size_bytes,
        "payload_json": _json_dumps(payload),
    }


def _duration_candidates(rows: list[sqlite3.Row], keys: tuple[str, ...]) -> list[float]:
    values: list[float] = []
    for row in rows:
        for key in keys:
            value = _as_float(row[key])
            if value is not None and value >= 0:
                values.append(value)
                break
    return values


def _latest_nonempty(rows: list[sqlite3.Row], key: str) -> Any:
    for row in reversed(rows):
        value = row[key]
        if value not in (None, ""):
            return value
    return None


def _first_event_at(rows: list[sqlite3.Row], event_type: str) -> str | None:
    for row in rows:
        if row["event_type"] == event_type:
            return row["received_at"]
    return None


def _last_event_at(rows: list[sqlite3.Row], event_type: str) -> str | None:
    for row in reversed(rows):
        if row["event_type"] == event_type:
            return row["received_at"]
    return None


def recompute_play_session_rollup(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    session_id: str,
    updated_at: str | None = None,
) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT *
        FROM play_session_events
        WHERE user_id = ? AND session_id = ?
        ORDER BY received_at, id
        """,
        (user_id, session_id),
    ).fetchall()
    if not rows:
        conn.execute(
            "DELETE FROM play_session_rollups WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        return {}

    updated_at = updated_at or now_iso()
    first_seen_at = rows[0]["received_at"]
    last_seen_at = rows[-1]["received_at"]
    login_at = _first_event_at(rows, "login")
    logoff_at = _last_event_at(rows, "logoff")
    heartbeat_rows = [row for row in rows if row["event_type"] == "heartbeat"]
    logoff_rows = [row for row in rows if row["event_type"] == "logoff"]
    heartbeat_count = len(heartbeat_rows)
    event_count = len(rows)
    max_sequence = max((row["sequence"] for row in rows if row["sequence"] is not None), default=None)
    server_span_sec = _seconds_between(first_seen_at, last_seen_at)

    logoff_durations = _duration_candidates(logoff_rows, ("game_duration_sec",))
    logoff_duration_sec = logoff_durations[-1] if logoff_durations else None
    heartbeat_durations = _duration_candidates(
        heartbeat_rows,
        ("game_duration_sec", "foreground_duration_sec", "active_duration_sec"),
    )
    heartbeat_duration_sec = max(heartbeat_durations) if heartbeat_durations else None

    if logoff_duration_sec is not None:
        final_duration_sec = logoff_duration_sec
        duration_source = "logoff"
        status = "closed"
        confidence = "high"
        end_reason = "logoff"
    elif heartbeat_duration_sec is not None:
        final_duration_sec = heartbeat_duration_sec
        duration_source = "heartbeat_client"
        status = "open"
        confidence = "high"
        end_reason = "latest_heartbeat"
    elif server_span_sec > 0:
        final_duration_sec = server_span_sec
        duration_source = "server_span"
        status = "open"
        confidence = "medium" if heartbeat_count else "low"
        end_reason = "no_logoff"
    else:
        final_duration_sec = 0.0
        duration_source = "login_only"
        status = "open"
        confidence = "low"
        end_reason = "single_event"

    rollup = {
        "user_id": user_id,
        "session_id": session_id,
        "player_session_id": _latest_nonempty(rows, "player_session_id"),
        "player_id": _latest_nonempty(rows, "player_id"),
        "client_version": _latest_nonempty(rows, "client_version"),
        "release_version": _latest_nonempty(rows, "release_version"),
        "country": _latest_nonempty(rows, "country"),
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
        "login_at": login_at,
        "logoff_at": logoff_at,
        "last_client_sent_at": _latest_nonempty(rows, "client_sent_at"),
        "heartbeat_count": heartbeat_count,
        "event_count": event_count,
        "max_sequence": max_sequence,
        "logoff_duration_sec": logoff_duration_sec,
        "heartbeat_duration_sec": heartbeat_duration_sec,
        "server_span_sec": server_span_sec,
        "estimated_tail_sec": 0.0,
        "final_duration_sec": final_duration_sec,
        "duration_source": duration_source,
        "status": status,
        "confidence": confidence,
        "end_reason": end_reason,
        "app_state": _latest_nonempty(rows, "app_state"),
        "last_gameplay_event_at": _latest_nonempty(rows, "last_gameplay_event_at"),
        "last_event_type": rows[-1]["event_type"],
        "updated_at": updated_at,
    }
    conn.execute(
        """
        INSERT INTO play_session_rollups (
            user_id, session_id, player_session_id, player_id, client_version,
            release_version, country, first_seen_at, last_seen_at, login_at,
            logoff_at, last_client_sent_at, heartbeat_count, event_count,
            max_sequence, logoff_duration_sec, heartbeat_duration_sec,
            server_span_sec, estimated_tail_sec, final_duration_sec,
            duration_source, status, confidence, end_reason, app_state,
            last_gameplay_event_at, last_event_type, updated_at
        ) VALUES (
            :user_id, :session_id, :player_session_id, :player_id, :client_version,
            :release_version, :country, :first_seen_at, :last_seen_at, :login_at,
            :logoff_at, :last_client_sent_at, :heartbeat_count, :event_count,
            :max_sequence, :logoff_duration_sec, :heartbeat_duration_sec,
            :server_span_sec, :estimated_tail_sec, :final_duration_sec,
            :duration_source, :status, :confidence, :end_reason, :app_state,
            :last_gameplay_event_at, :last_event_type, :updated_at
        )
        ON CONFLICT(user_id, session_id) DO UPDATE SET
            player_session_id = excluded.player_session_id,
            player_id = excluded.player_id,
            client_version = excluded.client_version,
            release_version = excluded.release_version,
            country = excluded.country,
            first_seen_at = excluded.first_seen_at,
            last_seen_at = excluded.last_seen_at,
            login_at = excluded.login_at,
            logoff_at = excluded.logoff_at,
            last_client_sent_at = excluded.last_client_sent_at,
            heartbeat_count = excluded.heartbeat_count,
            event_count = excluded.event_count,
            max_sequence = excluded.max_sequence,
            logoff_duration_sec = excluded.logoff_duration_sec,
            heartbeat_duration_sec = excluded.heartbeat_duration_sec,
            server_span_sec = excluded.server_span_sec,
            estimated_tail_sec = excluded.estimated_tail_sec,
            final_duration_sec = excluded.final_duration_sec,
            duration_source = excluded.duration_source,
            status = excluded.status,
            confidence = excluded.confidence,
            end_reason = excluded.end_reason,
            app_state = excluded.app_state,
            last_gameplay_event_at = excluded.last_gameplay_event_at,
            last_event_type = excluded.last_event_type,
            updated_at = excluded.updated_at
        """,
        rollup,
    )
    return rollup


def record_play_session_event(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, Any],
    headers: dict[str, Any],
    event_type: str,
    received_at: str,
    payload_size_bytes: int = 0,
    ensure_schema: bool = True,
) -> dict[str, Any]:
    if ensure_schema:
        ensure_playtime_schema(conn)
    event = _build_event(
        payload=payload,
        headers=headers,
        event_type=event_type,
        received_at=received_at,
        payload_size_bytes=payload_size_bytes,
    )
    conn.execute(
        """
        INSERT INTO play_session_events (
            dedupe_key, event_type, received_at, user_id, session_id,
            player_session_id, player_id, client_version, release_version, country,
            client_sent_at, sequence, game_duration_sec, foreground_duration_sec,
            active_duration_sec, app_state, last_gameplay_event_at, outbox_id,
            payload_size_bytes, payload_json, updated_at
        ) VALUES (
            :dedupe_key, :event_type, :received_at, :user_id, :session_id,
            :player_session_id, :player_id, :client_version, :release_version, :country,
            :client_sent_at, :sequence, :game_duration_sec, :foreground_duration_sec,
            :active_duration_sec, :app_state, :last_gameplay_event_at, :outbox_id,
            :payload_size_bytes, :payload_json, CURRENT_TIMESTAMP
        )
        ON CONFLICT(dedupe_key) DO UPDATE SET
            received_at = excluded.received_at,
            player_session_id = excluded.player_session_id,
            player_id = excluded.player_id,
            client_version = excluded.client_version,
            release_version = excluded.release_version,
            country = excluded.country,
            client_sent_at = excluded.client_sent_at,
            sequence = excluded.sequence,
            game_duration_sec = excluded.game_duration_sec,
            foreground_duration_sec = excluded.foreground_duration_sec,
            active_duration_sec = excluded.active_duration_sec,
            app_state = excluded.app_state,
            last_gameplay_event_at = excluded.last_gameplay_event_at,
            payload_size_bytes = excluded.payload_size_bytes,
            payload_json = excluded.payload_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        event,
    )
    return recompute_play_session_rollup(
        conn,
        user_id=event["user_id"],
        session_id=event["session_id"],
        updated_at=received_at,
    )


def record_play_session_event_to_db(
    *,
    db_path: str | Path,
    payload: dict[str, Any],
    headers: dict[str, Any],
    event_type: str,
    received_at: str,
    payload_size_bytes: int = 0,
) -> dict[str, Any]:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        ensure_playtime_schema_once(conn, db_path)
        rollup = record_play_session_event(
            conn,
            payload=payload,
            headers=headers,
            event_type=event_type,
            received_at=received_at,
            payload_size_bytes=payload_size_bytes,
            ensure_schema=False,
        )
        conn.commit()
    return rollup
