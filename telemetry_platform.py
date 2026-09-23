"""Stable client-origin metadata. Never infer a client platform from server OS/version."""

PLATFORMS = {"webgl", "windows", "editor", "other", "unknown", "unattributed"}


def normalize_platform(value):
    return value if isinstance(value, str) and value in PLATFORMS else "unknown"


def client_metadata(payload=None, headers=None):
    payload = payload if isinstance(payload, dict) else {}
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    telemetry = payload.get("gameplay_telemetry")
    meta = telemetry.get("session_meta") if isinstance(telemetry, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    environment = payload.get("client_environment")
    environment = environment if isinstance(environment, dict) else {}
    platform = normalize_platform(headers.get("x-client-platform") or payload.get("client_platform")
                                  or meta.get("client_platform") or environment.get("platform"))
    development = headers.get("x-is-development-build", payload.get("is_development_build",
                              meta.get("is_development_build", False)))
    return {"client_platform": platform,
            "distribution_channel": normalize_channel(headers.get("x-distribution-channel") or payload.get("distribution_channel") or meta.get("distribution_channel") or environment.get("distribution_channel")),
            "is_development_build": int(development is True or str(development).lower() in ("true", "1"))}


def ensure_platform_columns(conn, tables):
    """Additive migration: unknown historical origins remain explicitly unknown."""
    for table in tables:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not columns:
            continue
        for name, sql_type in (("client_platform", "TEXT NOT NULL DEFAULT 'unknown'"),
                               ("is_development_build", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_platform ON {table}(client_platform)")


def normalize_channel(value):
    """Only explicit client markers identify a distribution; old version prefixes do not."""
    return value if isinstance(value, str) and value in {"internal", "steam", "taptap", "web"} else "unknown"


def ensure_channel_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS analytics_session_channels (
        user_id TEXT NOT NULL, session_id TEXT NOT NULL, distribution_channel TEXT NOT NULL,
        PRIMARY KEY(user_id,session_id))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_channel ON analytics_session_channels(distribution_channel)")


def record_session_channel(conn, user, session, metadata):
    """First explicit channel wins; missing markers never erase a known session origin."""
    channel = normalize_channel(metadata.get("distribution_channel"))
    if channel == "unknown" or not user or not session:
        return
    conn.execute("INSERT OR IGNORE INTO analytics_session_channels VALUES (?,?,?)", (user,session,channel))
