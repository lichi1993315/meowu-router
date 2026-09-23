"""Shared Grafana facts: origin is per session, first entry is global, clocks are UTC."""
from telemetry_events import ensure_event_schema
from telemetry_platform import ensure_platform_columns

VIEWS = """
DROP VIEW IF EXISTS analytics_sessions;
CREATE VIEW analytics_sessions AS
WITH candidates AS (
 SELECT user_id, session_id, player_session_id, client_platform, client_version, release_version,
        is_development_build, COALESCE(login_at,first_seen_at) started_at, last_seen_at ended_at,
        final_duration_sec duration_sec, idle_duration_sec, afk_duration_sec,
        gameplay_active_duration_sec, duration_source, confidence, status,
        0 source_rank
 FROM play_session_rollups
 UNION ALL
 SELECT user_id,session_id,player_session_id,client_platform,client_version,release_version,
        is_development_build,real_time_started_iso,real_time_ended_iso,game_duration_sec,
        NULL,NULL,NULL,'legacy_snapshot','unknown','closed',1
 FROM gameplay_sessions
), ranked AS (
 SELECT *, ROW_NUMBER() OVER (PARTITION BY user_id,COALESCE(NULLIF(player_session_id,''),session_id)
 ORDER BY source_rank,ended_at DESC) rn FROM candidates
)
SELECT r.*, (SELECT m.playtest_id FROM analytics_session_playtests m WHERE m.user_id=r.user_id AND m.session_id=r.session_id) playtest_id, COALESCE(u.is_developer,0) is_developer FROM ranked r
LEFT JOIN user_sessions u ON r.user_id=u.user_id
WHERE rn=1 AND r.user_id NOT IN ('','unknown','anonymous','anonymous_user');

DROP VIEW IF EXISTS analytics_events;
CREATE VIEW analytics_events AS
SELECT f.*, (SELECT m.playtest_id FROM analytics_session_playtests m WHERE m.user_id=f.user_id AND m.session_id=f.session_id) playtest_id,COALESCE(u.is_developer,0) is_developer FROM analytics_event_facts f
LEFT JOIN user_sessions u ON f.user_id=u.user_id;

DROP VIEW IF EXISTS analytics_activity;
CREATE VIEW analytics_activity AS
SELECT DISTINCT user_id,session_id,client_platform,client_version,release_version,
       is_development_build,date(COALESCE(NULLIF(client_sent_at,''),received_at),'+8 hours') activity_day
FROM play_session_events
WHERE event_type IN ('login','heartbeat') AND COALESCE(app_state,'foreground') NOT IN ('background','paused','quitting')
UNION
SELECT user_id,session_id,client_platform,client_version,release_version,is_development_build,
       date(started_at,'+8 hours') FROM analytics_sessions;

DROP VIEW IF EXISTS analytics_first_entry;
CREATE VIEW analytics_first_entry AS
WITH eligible AS (
 SELECT *, 'production' cohort_scope FROM analytics_sessions
 WHERE client_platform!='editor' AND is_development_build=0 AND is_developer=0
 UNION ALL
 SELECT *, 'all' cohort_scope FROM analytics_sessions
)
SELECT user_id,started_at first_entry_at,date(started_at,'+8 hours') first_day,client_platform first_platform,
       release_version first_release,cohort_scope,
       (client_platform='editor' OR is_development_build=1 OR is_developer=1) first_is_test FROM (
 SELECT *,ROW_NUMBER() OVER (PARTITION BY user_id,cohort_scope ORDER BY julianday(started_at),session_id) first_rank
 FROM eligible WHERE started_at IS NOT NULL
) WHERE first_rank=1;
"""


def ensure_analytics_schema(conn):
    """Run during migration/importer startup, never per incoming gameplay event."""
    ensure_event_schema(conn)
    ensure_platform_columns(conn, ("gameplay_sessions", "gameplay_events", "gameplay_ai_calls",
                                   "conversations", "play_session_events", "play_session_rollups"))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gameplay_event_identity ON gameplay_events(json_extract(meta_json,'$.event_id'))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gameplay_event_time ON gameplay_events(event_real_time_iso,client_platform)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_live_user_kind_time ON gameplay_live_events(user_id,event_type,occurred_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gameplay_user_kind_time ON gameplay_events(user_id,event_type,event_real_time_iso)")
    from analytics_facts import migrate_facts
    migrate_facts(conn)
    conn.executescript(VIEWS)
