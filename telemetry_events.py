"""Idempotent, bounded ingestion for gameplay batches. ACK only follows commit."""
import json
import sqlite3
from datetime import datetime, timezone
from telemetry_platform import client_metadata, ensure_channel_schema, record_session_channel
from analytics_facts import ensure_facts, upsert_fact
from version_utils import release_version_from_client_version


def ensure_event_schema(conn):
    ensure_facts(conn)
    ensure_channel_schema(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS gameplay_live_events (
        event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
        player_session_id TEXT, client_platform TEXT NOT NULL, client_version TEXT,
        is_development_build INTEGER NOT NULL DEFAULT 0, occurred_at TEXT NOT NULL,
        received_at TEXT NOT NULL, event_type TEXT NOT NULL, event_json TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_live_time_platform ON gameplay_live_events(occurred_at,client_platform)")


def accept_batch(db_path, payload, headers):
    """Validate all events before writing; identical retries are safe."""
    events = payload.get("events")
    if not isinstance(events, list) or not 1 <= len(events) <= 100:
        raise ValueError("events must contain 1..100 records")
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 262144:
        raise ValueError("batch exceeds 256 KiB")
    headers = {k.lower(): v for k, v in headers.items()}
    user = headers.get("x-user-id") or payload.get("user_id")
    session = headers.get("x-session-id") or payload.get("session_id")
    if not user or not session:
        raise ValueError("user and session required")
    metadata = client_metadata(payload, headers)
    received = datetime.now(timezone.utc).isoformat()
    rows = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        eid, occurred, kind = event.get("event_id"), event.get("event_real_time_iso"), event.get("event_type")
        if not all(isinstance(x, str) and x for x in (eid, occurred, kind)) or len(eid) > 128:
            raise ValueError("event id, timestamp and type required")
        stamp = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("timestamp timezone required")
        rows.append((eid, user, session, payload.get("player_session_id"), metadata["client_platform"],
                     payload.get("client_version"), metadata["is_development_build"],
                     stamp.astimezone(timezone.utc).isoformat(), received, kind,
                     json.dumps(event, ensure_ascii=False)))
    with sqlite3.connect(db_path, timeout=10) as conn:
        ensure_event_schema(conn)
        # A conflicting identity is a permanent validation error, never a silent dedupe.
        for row in rows:
            existing = conn.execute("SELECT user_id,session_id FROM gameplay_live_events WHERE event_id=?", (row[0],)).fetchone()
            projected = conn.execute("SELECT user_id,session_id FROM analytics_event_facts WHERE event_id=?", (row[0],)).fetchone()
            if projected and projected != (user, session):
                raise ValueError("event id belongs to a different session")
            if existing and existing != (user, session):
                raise ValueError("event id belongs to a different session")
        record_session_channel(conn, user, session, metadata)
        conn.executemany("INSERT OR IGNORE INTO gameplay_live_events VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        for event in events:
            upsert_fact(conn,event,user,session,metadata,received,payload.get('player_session_id'),payload.get('client_version'),payload.get('release_version') or release_version_from_client_version(payload.get('client_version')))
    return {"accepted_event_ids": [r[0] for r in rows]}
