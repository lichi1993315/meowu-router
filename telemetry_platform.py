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
