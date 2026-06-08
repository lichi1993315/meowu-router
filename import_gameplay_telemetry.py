#!/usr/bin/env python3
"""
Import gameplay telemetry into SQLite for Grafana analytics.

The importer supports two sources:
1. Raw gameplay telemetry ingest JSON files in a data directory.
2. Live session log files under output/*/session-*.jsonl where gameplay
   telemetry is embedded in logoff.payload.gameplay_telemetry.

Imports are idempotent per player_session_id when available, and per source
file/session_id as a fallback. Raw ingest files are tracked in
gameplay_telemetry_ingest so missing sessions can be diagnosed by ingest state.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DB_PATH = Path(os.getenv("DB_PATH", "/app/data/conversations.db"))
TELEMETRY_DIR = Path(os.getenv("GAMEPLAY_TELEMETRY_DIR", "/app/data/gameplay_telemetry"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/app/output"))
STATE_PATH = Path(os.getenv("GAMEPLAY_IMPORTER_STATE_PATH", "/app/data/gameplay_importer_state.json"))
IMPORT_INTERVAL_SEC = int(os.getenv("GAMEPLAY_IMPORT_INTERVAL_SEC", "300"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
STATE_SCHEMA_VERSION = 5

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("gameplay_importer")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gameplay_sessions (
    source_file TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    player_session_id TEXT,
    is_dev INTEGER DEFAULT 0,
    client_version TEXT,
    nickname TEXT,
    country TEXT,
    real_time_started_iso TEXT,
    real_time_ended_iso TEXT,
    imported_at TEXT NOT NULL,
    game_duration_sec REAL DEFAULT 0,
    game_day_start INTEGER,
    game_day_end INTEGER,
    game_days_total INTEGER,
    island_level_max INTEGER,
    money_start INTEGER,
    money_end INTEGER,
    money_total_earned INTEGER,
    money_total_spent INTEGER,
    money_net_delta INTEGER,
    money_reconciliation_ok INTEGER,
    pluma_luoqiu_total INTEGER,
    new_player_task_progress_json TEXT,
    session_meta_json TEXT,
    ai_usage_source TEXT,
    ai_request_count INTEGER DEFAULT 0,
    ai_response_count INTEGER DEFAULT 0,
    ai_token_record_count INTEGER DEFAULT 0,
    ai_input_tokens INTEGER DEFAULT 0,
    ai_output_tokens INTEGER DEFAULT 0,
    ai_total_tokens INTEGER DEFAULT 0,
    ai_cached_input_tokens INTEGER DEFAULT 0,
    ai_cache_read_input_tokens INTEGER DEFAULT 0,
    ai_cache_creation_input_tokens INTEGER DEFAULT 0,
    ai_billable_uncached_input_tokens INTEGER DEFAULT 0,
    ai_billable_cached_input_tokens INTEGER DEFAULT 0,
    ai_cache_hit_ratio REAL DEFAULT 0,
    ai_estimated_cost_usd REAL DEFAULT 0,
    ai_archive_total_consumed_tokens INTEGER,
    ai_models TEXT,
    ai_pricing_json TEXT,
    PRIMARY KEY (source_file, user_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_gameplay_sessions_user ON gameplay_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_gameplay_sessions_started ON gameplay_sessions(real_time_started_iso);

CREATE TABLE IF NOT EXISTS gameplay_telemetry_ingest (
    source_file TEXT PRIMARY KEY,
    ingest_id TEXT,
    endpoint TEXT,
    event_type TEXT,
    received_at TEXT,
    user_id TEXT,
    session_id TEXT,
    player_session_id TEXT,
    player_id TEXT,
    client_version TEXT,
    outbox_id TEXT,
    payload_size_bytes INTEGER DEFAULT 0,
    import_status TEXT NOT NULL DEFAULT 'pending',
    import_error TEXT,
    sample_count INTEGER DEFAULT 0,
    session_count INTEGER DEFAULT 0,
    imported_at TEXT,
    raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_gameplay_telemetry_ingest_status
    ON gameplay_telemetry_ingest(import_status);
CREATE INDEX IF NOT EXISTS idx_gameplay_telemetry_ingest_player_session
    ON gameplay_telemetry_ingest(user_id, player_session_id);
CREATE INDEX IF NOT EXISTS idx_gameplay_telemetry_ingest_received
    ON gameplay_telemetry_ingest(received_at);

CREATE TABLE IF NOT EXISTS gameplay_days (
    source_file TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    player_session_id TEXT,
    is_dev INTEGER DEFAULT 0,
    client_version TEXT,
    game_day INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    day_end_completed INTEGER,
    energy_remaining_end INTEGER,
    energy_total_end INTEGER,
    cats_count INTEGER DEFAULT 0,
    cats_json TEXT,
    day_meta_json TEXT,
    PRIMARY KEY (source_file, user_id, session_id, game_day)
);

CREATE INDEX IF NOT EXISTS idx_gameplay_days_user_day ON gameplay_days(user_id, game_day);

CREATE TABLE IF NOT EXISTS gameplay_events (
    source_file TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    player_session_id TEXT,
    is_dev INTEGER DEFAULT 0,
    client_version TEXT,
    game_day INTEGER,
    event_index INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    event_type TEXT,
    event_game_minutes INTEGER,
    event_real_time_iso TEXT,
    actor_id TEXT,
    actor_name TEXT,
    actor_is_player INTEGER,
    island_level INTEGER,
    duration_minutes REAL,
    energy_cost REAL,
    meowu_output REAL,
    mounted_cats_count INTEGER,
    rod_id TEXT,
    rod_name TEXT,
    fish_id TEXT,
    fish_name TEXT,
    fish_rarity TEXT,
    fish_price REAL,
    fish_size_cm REAL,
    fish_size_label TEXT,
    seed_id TEXT,
    seed_name TEXT,
    crop_id TEXT,
    crop_name TEXT,
    crop_rarity TEXT,
    crop_price REAL,
    bug_id TEXT,
    bug_name TEXT,
    bug_rarity TEXT,
    bug_price REAL,
    recipe_id TEXT,
    recipe_name TEXT,
    building_id TEXT,
    building_name TEXT,
    building_sub_type TEXT,
    item_id TEXT,
    item_name TEXT,
    money_spent REAL,
    earned_money REAL,
    adopted_cat_id TEXT,
    adopted_cat_name TEXT,
    adopt_source TEXT,
    region TEXT,
    meta_json TEXT,
    payload_json TEXT,
    PRIMARY KEY (source_file, user_id, session_id, event_index)
);

CREATE INDEX IF NOT EXISTS idx_gameplay_events_user_type ON gameplay_events(user_id, event_type);
CREATE INDEX IF NOT EXISTS idx_gameplay_events_user_day ON gameplay_events(user_id, game_day);
CREATE INDEX IF NOT EXISTS idx_gameplay_events_type ON gameplay_events(event_type);

CREATE TABLE IF NOT EXISTS gameplay_ai_calls (
    source_file TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    player_session_id TEXT,
    event_index INTEGER NOT NULL,
    is_dev INTEGER DEFAULT 0,
    client_version TEXT,
    game_day INTEGER,
    event_game_minutes INTEGER,
    event_real_time_iso TEXT,
    actor_id TEXT,
    actor_name TEXT,
    model TEXT,
    mode TEXT,
    tag TEXT,
    toolset_version TEXT,
    prompt_cache_key TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    cached_input_tokens INTEGER DEFAULT 0,
    cache_read_input_tokens INTEGER DEFAULT 0,
    cache_creation_input_tokens INTEGER DEFAULT 0,
    billable_uncached_input_tokens INTEGER DEFAULT 0,
    billable_cached_input_tokens INTEGER DEFAULT 0,
    cache_hit_ratio REAL DEFAULT 0,
    input_usd_per_million_tokens REAL,
    output_usd_per_million_tokens REAL,
    cached_input_usd_per_million_tokens REAL,
    estimated_cost_usd REAL DEFAULT 0,
    request_message_count INTEGER,
    message_content_chars INTEGER,
    message_json_chars INTEGER,
    tool_count INTEGER,
    tool_schema_json_chars INTEGER,
    payload_json TEXT,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (source_file, user_id, session_id, event_index)
);

CREATE INDEX IF NOT EXISTS idx_gameplay_ai_calls_user ON gameplay_ai_calls(user_id);
CREATE INDEX IF NOT EXISTS idx_gameplay_ai_calls_session ON gameplay_ai_calls(user_id, session_id);
CREATE INDEX IF NOT EXISTS idx_gameplay_ai_calls_client_version ON gameplay_ai_calls(client_version);
CREATE INDEX IF NOT EXISTS idx_gameplay_ai_calls_real_time ON gameplay_ai_calls(event_real_time_iso);
"""


VIEW_SQL = """
DROP VIEW IF EXISTS gameplay_player_summary;
CREATE VIEW IF NOT EXISTS gameplay_player_summary AS
WITH player_sessions AS (
    SELECT
        s.user_id,
        MAX(COALESCE(s.is_dev, 0)) AS is_dev,
        MAX(NULLIF(s.nickname, '')) AS telemetry_nickname,
        MIN(date(s.real_time_started_iso)) AS first_login_date,
        MIN(s.real_time_started_iso) AS first_login_iso,
        MAX(date(s.real_time_started_iso)) AS latest_login_date,
        MAX(s.real_time_started_iso) AS latest_login_iso,
        MAX(s.real_time_started_iso) AS latest_session_started_iso,
        GROUP_CONCAT(DISTINCT COALESCE(NULLIF(s.client_version, ''), '(empty)')) AS client_versions,
        SUM(COALESCE(s.game_duration_sec, 0)) AS total_play_duration_sec,
        MAX(COALESCE(s.game_days_total, s.game_day_end, 0)) AS play_days_total,
        MAX(COALESCE(s.island_level_max, 0)) AS island_level_max
    FROM gameplay_sessions s
    GROUP BY s.user_id
),
event_island AS (
    SELECT
        user_id,
        MAX(COALESCE(island_level, 0)) AS island_level_max
    FROM gameplay_events
    GROUP BY user_id
),
event_cats AS (
    SELECT
        user_id,
        COUNT(DISTINCT COALESCE(
            NULLIF(json_extract(payload_json, '$.cat_id'), ''),
            NULLIF(actor_id, '')
        )) AS cats_count
    FROM gameplay_events
    WHERE
        event_type = 'cat_tool_completed'
        OR json_extract(payload_json, '$.cat_id') IS NOT NULL
        OR json_extract(payload_json, '$.cat_name') IS NOT NULL
    GROUP BY user_id
),
latest_session AS (
    SELECT
        user_id,
        COALESCE(
            money_end,
            money_start + COALESCE(money_total_earned, 0) - COALESCE(money_total_spent, 0),
            money_start
        ) AS total_money,
        country,
        ROW_NUMBER() OVER (
            PARTITION BY user_id
            ORDER BY
                CASE WHEN real_time_started_iso IS NULL THEN 1 ELSE 0 END,
                real_time_started_iso DESC,
                imported_at DESC
        ) AS rn
    FROM gameplay_sessions
),
latest_day AS (
    SELECT
        user_id,
        cats_count,
        ROW_NUMBER() OVER (
            PARTITION BY user_id
            ORDER BY game_day DESC, imported_at DESC
        ) AS rn
    FROM gameplay_days
)
SELECT
    ps.user_id,
    ps.is_dev,
    COALESCE(
        NULLIF(us.nickname, ''),
        NULLIF(us.player_name, ''),
        ps.telemetry_nickname,
        ps.user_id
    ) AS nickname,
    ps.first_login_date,
    ps.first_login_iso,
    ps.latest_login_date,
    ps.latest_login_iso,
    ps.latest_session_started_iso,
    ps.client_versions,
    ROUND(ps.total_play_duration_sec, 2) AS total_play_duration_sec,
    ps.play_days_total,
    MAX(ps.island_level_max, COALESCE(ei.island_level_max, 0)) AS island_level_max,
    COALESCE(ls.total_money, 0) AS total_money,
    COALESCE(ls.country, '') AS country,
    COALESCE(NULLIF(ld.cats_count, 0), ec.cats_count, 0) AS cats_count
FROM player_sessions ps
LEFT JOIN user_sessions us
    ON us.user_id = ps.user_id
LEFT JOIN event_island ei
    ON ei.user_id = ps.user_id
LEFT JOIN event_cats ec
    ON ec.user_id = ps.user_id
LEFT JOIN latest_session ls
    ON ls.user_id = ps.user_id AND ls.rn = 1
LEFT JOIN latest_day ld
    ON ld.user_id = ps.user_id AND ld.rn = 1;

DROP VIEW IF EXISTS gameplay_ai_user_summary;
CREATE VIEW IF NOT EXISTS gameplay_ai_user_summary AS
SELECT
    s.user_id,
    MAX(COALESCE(s.is_dev, 0)) AS is_dev,
    COALESCE(
        NULLIF(us.nickname, ''),
        NULLIF(us.player_name, ''),
        MAX(NULLIF(s.nickname, '')),
        s.user_id
    ) AS nickname,
    GROUP_CONCAT(DISTINCT COALESCE(NULLIF(s.client_version, ''), '(empty)')) AS client_versions,
    COUNT(*) AS ai_session_count,
    SUM(COALESCE(s.ai_request_count, 0)) AS ai_request_count,
    SUM(COALESCE(s.ai_response_count, 0)) AS ai_response_count,
    SUM(COALESCE(s.ai_token_record_count, 0)) AS ai_token_record_count,
    SUM(COALESCE(s.ai_input_tokens, 0)) AS ai_input_tokens,
    SUM(COALESCE(s.ai_output_tokens, 0)) AS ai_output_tokens,
    SUM(COALESCE(s.ai_total_tokens, 0)) AS ai_total_tokens,
    SUM(COALESCE(s.ai_cached_input_tokens, 0)) AS ai_cached_input_tokens,
    SUM(COALESCE(s.ai_cache_read_input_tokens, 0)) AS ai_cache_read_input_tokens,
    SUM(COALESCE(s.ai_cache_creation_input_tokens, 0)) AS ai_cache_creation_input_tokens,
    SUM(COALESCE(s.ai_billable_uncached_input_tokens, 0)) AS ai_billable_uncached_input_tokens,
    SUM(COALESCE(s.ai_billable_cached_input_tokens, 0)) AS ai_billable_cached_input_tokens,
    CASE
        WHEN SUM(COALESCE(s.ai_input_tokens, 0)) > 0
        THEN ROUND(
            CAST(SUM(COALESCE(s.ai_cached_input_tokens, 0)) AS REAL)
            / SUM(COALESCE(s.ai_input_tokens, 0)),
            4
        )
        ELSE 0
    END AS ai_cache_hit_ratio,
    ROUND(SUM(COALESCE(s.ai_estimated_cost_usd, 0)), 6) AS ai_estimated_cost_usd,
    MIN(s.real_time_started_iso) AS first_ai_session_iso,
    MAX(s.real_time_started_iso) AS latest_ai_session_iso
FROM gameplay_sessions s
LEFT JOIN user_sessions us
    ON us.user_id = s.user_id
WHERE
    COALESCE(s.ai_total_tokens, 0) > 0
    OR COALESCE(s.ai_response_count, 0) > 0
GROUP BY s.user_id;
"""


@dataclass
class ImportState:
    files: dict[str, int]
    schema_version: int = 0

    @classmethod
    def load(cls, path: Path) -> "ImportState":
        if not path.exists():
            return cls(files={})
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load state file %s: %s", path, exc)
            return cls(files={})
        files = data.get("files", {})
        if not isinstance(files, dict):
            files = {}
        schema_version = as_int(data.get("schema_version")) or 0
        normalized = {}
        for key, value in files.items():
            try:
                normalized[str(key)] = int(value)
            except Exception:
                continue
        return cls(files=normalized, schema_version=schema_version)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": self.schema_version,
            "files": self.files,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def as_nonempty_str(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def first_nonempty_str(*values: Any) -> str:
    for value in values:
        text = as_nonempty_str(value)
        if text:
            return text
    return ""


def int0(value: Any) -> int:
    return as_int(value) or 0


def float0(value: Any) -> float:
    return as_float(value) or 0.0


def first_int(raw: dict[str, Any], keys: Iterable[str]) -> int:
    for key in keys:
        value = as_int(raw.get(key))
        if value is not None:
            return value
    return 0


def first_float(raw: dict[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        value = as_float(raw.get(key))
        if value is not None:
            return value
    return None


def calculate_ai_cost_usd(usage: dict[str, Any]) -> float:
    input_rate = as_float(usage.get("input_usd_per_million_tokens"))
    output_rate = as_float(usage.get("output_usd_per_million_tokens"))
    cached_rate = as_float(usage.get("cached_input_usd_per_million_tokens"))
    if input_rate is None or output_rate is None:
        return 0.0

    cached_input = int0(usage.get("billable_cached_input_tokens"))
    uncached_input = int0(usage.get("billable_uncached_input_tokens"))
    if not cached_input and not uncached_input:
        cached_input = int0(usage.get("cached_input_tokens"))
        uncached_input = max(0, int0(usage.get("input_tokens")) - cached_input)

    cached_cost = cached_input * cached_rate if cached_rate is not None else 0.0
    return (
        (uncached_input * input_rate)
        + cached_cost
        + (int0(usage.get("output_tokens")) * output_rate)
    ) / 1_000_000


def normalize_ai_token_usage(
    raw: dict[str, Any],
    *,
    source: str,
    default_pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if default_pricing is None:
        default_pricing = {}

    prompt_details = raw.get("prompt_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = {}

    cached_input = (
        first_int(raw, ("session_cached_input_tokens", "cached_input_tokens", "cached_tokens", "cache_read_input_tokens"))
        or int0(prompt_details.get("cached_tokens"))
    )
    usage = {
        "source": source,
        "archive_total_consumed_tokens": int0(raw.get("archive_total_consumed_tokens")),
        "input_tokens": first_int(raw, ("session_input_tokens", "input_tokens", "prompt_tokens")),
        "output_tokens": first_int(raw, ("session_output_tokens", "output_tokens", "completion_tokens")),
        "total_tokens": first_int(raw, ("session_total_tokens", "total_tokens")),
        "cached_input_tokens": cached_input,
        "cache_read_input_tokens": first_int(
            raw,
            ("session_cache_read_input_tokens", "cache_read_input_tokens", "cache_read_tokens"),
        ) or cached_input,
        "cache_creation_input_tokens": first_int(
            raw,
            ("session_cache_creation_input_tokens", "cache_creation_input_tokens"),
        ),
        "billable_uncached_input_tokens": first_int(
            raw,
            ("session_billable_uncached_input_tokens", "billable_uncached_input_tokens"),
        ),
        "billable_cached_input_tokens": first_int(
            raw,
            ("session_billable_cached_input_tokens", "billable_cached_input_tokens"),
        ),
        "input_usd_per_million_tokens": first_float(
            raw,
            ("input_usd_per_million_tokens",),
        ),
        "output_usd_per_million_tokens": first_float(
            raw,
            ("output_usd_per_million_tokens",),
        ),
        "cached_input_usd_per_million_tokens": first_float(
            raw,
            ("cached_input_usd_per_million_tokens",),
        ),
    }
    for key in (
        "input_usd_per_million_tokens",
        "output_usd_per_million_tokens",
        "cached_input_usd_per_million_tokens",
    ):
        if usage[key] is None:
            usage[key] = as_float(default_pricing.get(key))

    if not usage["total_tokens"]:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    if not usage["billable_cached_input_tokens"]:
        usage["billable_cached_input_tokens"] = usage["cached_input_tokens"]
    if not usage["billable_uncached_input_tokens"] and usage["input_tokens"]:
        usage["billable_uncached_input_tokens"] = max(
            0,
            usage["input_tokens"] - usage["billable_cached_input_tokens"],
        )
    usage["cache_hit_ratio"] = (
        round(usage["cached_input_tokens"] / usage["input_tokens"], 4)
        if usage["input_tokens"]
        else 0.0
    )
    usage["estimated_cost_usd"] = calculate_ai_cost_usd(usage)
    return usage


def is_nonzero_ai_usage(usage: dict[str, Any]) -> bool:
    return any(int0(usage.get(key)) for key in ("input_tokens", "output_tokens", "total_tokens"))


def session_ai_pricing(session_meta: dict[str, Any]) -> dict[str, Any]:
    raw = session_meta.get("ai_token_usage")
    if not isinstance(raw, dict):
        return {}
    return {
        "input_usd_per_million_tokens": raw.get("input_usd_per_million_tokens"),
        "output_usd_per_million_tokens": raw.get("output_usd_per_million_tokens"),
        "cached_input_usd_per_million_tokens": raw.get("cached_input_usd_per_million_tokens"),
    }


def extract_event_ai_usage(
    payload: dict[str, Any],
    *,
    default_pricing: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    response_stats = payload.get("response_stats")
    raw = response_stats if isinstance(response_stats, dict) else payload
    usage = normalize_ai_token_usage(raw, source="event_payload", default_pricing=default_pricing)

    for key in (
        "cache_creation_input_tokens",
        "billable_uncached_input_tokens",
        "billable_cached_input_tokens",
        "input_usd_per_million_tokens",
        "output_usd_per_million_tokens",
        "cached_input_usd_per_million_tokens",
    ):
        if not usage.get(key) and payload.get(key) is not None:
            usage[key] = payload.get(key)

    usage["estimated_cost_usd"] = calculate_ai_cost_usd(usage)
    if not is_nonzero_ai_usage(usage):
        return None
    return usage


def aggregate_ai_usages(usages: list[dict[str, Any]]) -> dict[str, Any]:
    aggregate: dict[str, Any] = {
        "source": "event_payloads",
        "archive_total_consumed_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "billable_uncached_input_tokens": 0,
        "billable_cached_input_tokens": 0,
        "estimated_cost_usd": 0.0,
    }
    for usage in usages:
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "billable_uncached_input_tokens",
            "billable_cached_input_tokens",
            "archive_total_consumed_tokens",
        ):
            aggregate[key] += int0(usage.get(key))
        aggregate["estimated_cost_usd"] += float0(usage.get("estimated_cost_usd"))
    if not aggregate["total_tokens"]:
        aggregate["total_tokens"] = aggregate["input_tokens"] + aggregate["output_tokens"]
    if not aggregate["billable_cached_input_tokens"]:
        aggregate["billable_cached_input_tokens"] = aggregate["cached_input_tokens"]
    if not aggregate["billable_uncached_input_tokens"] and aggregate["input_tokens"]:
        aggregate["billable_uncached_input_tokens"] = max(
            0,
            aggregate["input_tokens"] - aggregate["billable_cached_input_tokens"],
        )
    aggregate["cache_hit_ratio"] = (
        round(aggregate["cached_input_tokens"] / aggregate["input_tokens"], 4)
        if aggregate["input_tokens"]
        else 0.0
    )
    return aggregate


def select_session_ai_usage(session_meta: dict[str, Any], event_usages: list[dict[str, Any]]) -> dict[str, Any]:
    raw = session_meta.get("ai_token_usage")
    if isinstance(raw, dict):
        usage = normalize_ai_token_usage(raw, source="session_meta.ai_token_usage")
        if is_nonzero_ai_usage(usage):
            return usage
    if event_usages:
        return aggregate_ai_usages(event_usages)
    return normalize_ai_token_usage({}, source="")


def ai_pricing_payload(usage: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "input_usd_per_million_tokens",
        "output_usd_per_million_tokens",
        "cached_input_usd_per_million_tokens",
    ):
        value = usage.get(key)
        if value is None:
            value = fallback.get(key)
        if value is not None:
            payload[key] = value
    return payload


def discover_json_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.json") if path.is_file())


def discover_session_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("session-*.jsonl") if path.is_file())


def is_gameplay_telemetry_ingest_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    ingest = payload.get("ingest")
    return isinstance(ingest, dict) and ingest.get("type") == "gameplay_telemetry_ingest"


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _short_error(error: str) -> str:
    return error[:2000]


def upsert_ingest_record(
    conn: sqlite3.Connection,
    *,
    source_file: str,
    payload: dict[str, Any] | None,
    import_status: str,
    import_error: str = "",
    sample_count: int = 0,
    session_count: int = 0,
    imported_at: str | None = None,
) -> None:
    ingest = payload.get("ingest") if isinstance(payload, dict) else {}
    if not isinstance(ingest, dict):
        ingest = {}

    conn.execute(
        """
        INSERT OR REPLACE INTO gameplay_telemetry_ingest (
            source_file, ingest_id, endpoint, event_type, received_at,
            user_id, session_id, player_session_id, player_id, client_version,
            outbox_id, payload_size_bytes, import_status, import_error,
            sample_count, session_count, imported_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_file,
            as_nonempty_str(ingest.get("id")),
            as_nonempty_str(ingest.get("endpoint")),
            as_nonempty_str(ingest.get("event_type")),
            as_nonempty_str(ingest.get("received_at")),
            as_nonempty_str(ingest.get("user_id") or (payload or {}).get("user_id")),
            as_nonempty_str(ingest.get("session_id") or (payload or {}).get("session_id")),
            as_nonempty_str(
                ingest.get("player_session_id") or (payload or {}).get("player_session_id")
            ),
            as_nonempty_str(ingest.get("player_id") or (payload or {}).get("player_id")),
            as_nonempty_str(ingest.get("client_version") or (payload or {}).get("client_version")),
            as_nonempty_str(ingest.get("outbox_id")),
            int0(ingest.get("payload_size_bytes")),
            import_status,
            _short_error(import_error),
            sample_count,
            session_count,
            imported_at,
            as_json(payload or {}),
        ),
    )


def record_ingest_import_failure(conn: sqlite3.Connection, path: Path, error: str) -> None:
    if path.suffix != ".json" or not _path_is_under(path, TELEMETRY_DIR):
        return
    source_file = str(path.resolve())
    imported_at = now_iso()
    cursor = conn.execute(
        """
        UPDATE gameplay_telemetry_ingest
        SET import_status = ?, import_error = ?, imported_at = ?
        WHERE source_file = ?
        """,
        ("failed", _short_error(error), imported_at, source_file),
    )
    if cursor.rowcount == 0:
        upsert_ingest_record(
            conn,
            source_file=source_file,
            payload=None,
            import_status="failed",
            import_error=error,
            imported_at=imported_at,
        )


def iter_samples(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("telemetry_samples"), list):
            for entry in payload["telemetry_samples"]:
                if isinstance(entry, dict):
                    yield entry
            return
        if isinstance(payload.get("gameplay_telemetry"), dict):
            yield payload
            return
        if isinstance(payload.get("session_meta"), dict) and isinstance(payload.get("days"), dict):
            yield {"user_id": payload.get("user_id") or "unknown", "gameplay_telemetry": payload}
            return
    if isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, dict):
                yield from iter_samples(entry)


def sample_from_session_record(session_record: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(session_record, dict) or session_record.get("type") != "session":
        return None

    sources = session_record.get("sources") or {}
    if not isinstance(sources, dict):
        return None

    selected_logoff = None
    fallback_logoff = None
    for source_name in ("launcher", "python"):
        source_bucket = sources.get(source_name) or {}
        if not isinstance(source_bucket, dict):
            continue
        logoff = source_bucket.get("logoff")
        if not isinstance(logoff, dict):
            continue
        if fallback_logoff is None:
            fallback_logoff = logoff
        payload = logoff.get("payload") or {}
        if isinstance(payload, dict) and isinstance(payload.get("gameplay_telemetry"), dict):
            selected_logoff = logoff
            break
    if not isinstance(selected_logoff, dict):
        selected_logoff = fallback_logoff
    if not isinstance(selected_logoff, dict):
        return None

    headers = selected_logoff.get("headers") or {}
    payload = selected_logoff.get("payload") or {}
    if not isinstance(headers, dict) or not isinstance(payload, dict):
        return None

    telemetry = payload.get("gameplay_telemetry")
    if not isinstance(telemetry, dict):
        return None
    session_meta = telemetry.get("session_meta")
    if not isinstance(session_meta, dict):
        session_meta = {}

    user_id = (
        session_record.get("user_id")
        or headers.get("x-user-id")
        or headers.get("X-User-ID")
        or "unknown"
    )
    sample = {
        "user_id": user_id,
        "username": payload.get("username"),
        "player_name": payload.get("username"),
        "country": headers.get("cf-ipcountry", ""),
        "client_version": payload.get("client_version")
        or headers.get("x-client-version")
        or headers.get("X-Client-Version"),
        "player_session_id": first_nonempty_str(
            payload.get("player_session_id"),
            payload.get("playerSessionId"),
            session_meta.get("player_session_id"),
            session_meta.get("playerSessionId"),
        ),
        "gameplay_telemetry": telemetry,
    }
    return sample


def infer_country(sample: dict[str, Any], session_meta: dict[str, Any]) -> str:
    for key in ("country", "country_code", "region", "country_or_region"):
        value = sample.get(key) or session_meta.get(key)
        if isinstance(value, str) and value:
            return value
    meta = sample.get("meta")
    if isinstance(meta, dict):
        for key in ("country", "region"):
            value = meta.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def infer_nickname(sample: dict[str, Any]) -> str:
    for key in ("nickname", "player_name", "username", "name", "user_name"):
        value = sample.get(key)
        if isinstance(value, str) and value:
            return value
    return sample.get("user_id") or "unknown"


def infer_is_dev(conn: sqlite3.Connection, user_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(is_developer, 0) FROM user_sessions WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def infer_client_version(sample: dict[str, Any], session_meta: dict[str, Any]) -> str:
    for value in (sample.get("client_version"), session_meta.get("client_version")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def infer_player_session_id(sample: dict[str, Any], session_meta: dict[str, Any]) -> str:
    return first_nonempty_str(
        sample.get("player_session_id"),
        sample.get("playerSessionId"),
        sample.get("player_session"),
        session_meta.get("player_session_id"),
        session_meta.get("playerSessionId"),
        session_meta.get("player_session"),
    )


def delete_existing_session_import(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    session_id: str,
    player_session_id: str,
) -> None:
    params: list[Any] = [user_id, session_id]
    conditions = ["(user_id = ? AND session_id = ?)"]
    if player_session_id:
        conditions.append("(user_id = ? AND player_session_id = ?)")
        params.extend([user_id, player_session_id])
    where_sql = " OR ".join(conditions)
    for table in ("gameplay_ai_calls", "gameplay_events", "gameplay_days", "gameplay_sessions"):
        conn.execute(f"DELETE FROM {table} WHERE {where_sql}", params)


def import_sample(
    conn: sqlite3.Connection,
    *,
    source_file: str,
    sample: dict[str, Any],
    imported_at: str,
) -> int:
    telemetry = sample.get("gameplay_telemetry")
    if not isinstance(telemetry, dict):
        return 0

    session_meta = telemetry.get("session_meta") or {}
    if not isinstance(session_meta, dict):
        session_meta = {}

    user_id = str(sample.get("user_id") or sample.get("player_id") or "unknown")
    nickname = infer_nickname(sample)
    is_dev = infer_is_dev(conn, user_id)
    country = infer_country(sample, session_meta)
    client_version = infer_client_version(sample, session_meta)
    if client_version and not session_meta.get("client_version"):
        session_meta = {**session_meta, "client_version": client_version}
    session_id = str(session_meta.get("session_id") or f"{user_id}:{source_file}")
    player_session_id = infer_player_session_id(sample, session_meta)
    if player_session_id and not session_meta.get("player_session_id"):
        session_meta = {**session_meta, "player_session_id": player_session_id}
    delete_existing_session_import(
        conn,
        user_id=user_id,
        session_id=session_id,
        player_session_id=player_session_id,
    )
    days = telemetry.get("days") or {}
    if not isinstance(days, dict):
        days = {}

    island_level_meta = as_int(session_meta.get("island_level_max")) or 0
    island_level_events = 0
    event_rows = []
    ai_call_rows = []
    ai_event_usages = []
    ai_request_count = 0
    ai_response_count = 0
    ai_models: set[str] = set()
    pricing = session_ai_pricing(session_meta)
    day_rows = []
    event_index = 0

    for day_key, day_obj in sorted(days.items(), key=lambda item: as_int(item[0]) or 0):
        if not isinstance(day_obj, dict):
            continue
        game_day = as_int(day_key) or as_int(day_obj.get("day_meta", {}).get("game_day")) or 0
        day_meta = day_obj.get("day_meta") or {}
        if not isinstance(day_meta, dict):
            day_meta = {}
        cats = day_meta.get("cats") or []
        cats_count = len(cats) if isinstance(cats, list) else 0
        day_rows.append(
            (
                source_file,
                user_id,
                session_id,
                player_session_id,
                is_dev,
                client_version,
                game_day,
                imported_at,
                as_int(day_meta.get("day_end_completed")),
                as_int(day_meta.get("energy_remaining_end")),
                as_int(day_meta.get("energy_total_end")),
                cats_count,
                as_json(cats),
                as_json(day_meta),
            )
        )

        events = day_obj.get("events") or []
        if not isinstance(events, list):
            events = []
        for event in events:
            if not isinstance(event, dict):
                continue
            event_index += 1
            payload = event.get("payload") or {}
            meta = event.get("meta") or {}
            actor = event.get("actor") or {}
            if not isinstance(payload, dict):
                payload = {}
            if not isinstance(meta, dict):
                meta = {}
            if not isinstance(actor, dict):
                actor = {}

            fish = payload.get("fish") or {}
            rod = payload.get("rod") or {}
            size = payload.get("size") or {}
            if not isinstance(fish, dict):
                fish = {}
            if not isinstance(rod, dict):
                rod = {}
            if not isinstance(size, dict):
                size = {}

            island_level = as_int(meta.get("island_level"))
            if island_level is not None:
                island_level_events = max(island_level_events, island_level)

            event_type = event.get("event_type")
            if event_type == "cat_agent_request":
                ai_request_count += 1
            if event_type == "cat_agent_response":
                ai_response_count += 1

            request_stats = payload.get("request_stats")
            if not isinstance(request_stats, dict):
                request_stats = {}
            prompt_profile = payload.get("prompt_profile")
            if not isinstance(prompt_profile, dict):
                prompt_profile = {}
            event_ai_usage = extract_event_ai_usage(payload, default_pricing=pricing)
            model = payload.get("model")
            if isinstance(model, str) and model:
                ai_models.add(model)
            elif isinstance(payload.get("response_model"), str) and payload.get("response_model"):
                ai_models.add(payload["response_model"])
            if event_ai_usage:
                ai_event_usages.append(event_ai_usage)
                ai_call_rows.append(
                    (
                        source_file,
                        user_id,
                        session_id,
                        player_session_id,
                        event_index,
                        is_dev,
                        client_version,
                        as_int(event.get("event_game_day")) or game_day,
                        as_int(event.get("event_game_minutes")),
                        event.get("timestamp"),
                        actor.get("agent_id"),
                        actor.get("agent_name") or actor.get("agent_id"),
                        model,
                        payload.get("mode"),
                        payload.get("tag"),
                        payload.get("toolset_version") or prompt_profile.get("toolset_version"),
                        request_stats.get("prompt_cache_key"),
                        int0(event_ai_usage.get("input_tokens")),
                        int0(event_ai_usage.get("output_tokens")),
                        int0(event_ai_usage.get("total_tokens")),
                        int0(event_ai_usage.get("cached_input_tokens")),
                        int0(event_ai_usage.get("cache_read_input_tokens")),
                        int0(event_ai_usage.get("cache_creation_input_tokens")),
                        int0(event_ai_usage.get("billable_uncached_input_tokens")),
                        int0(event_ai_usage.get("billable_cached_input_tokens")),
                        as_float(event_ai_usage.get("cache_hit_ratio")) or 0.0,
                        as_float(event_ai_usage.get("input_usd_per_million_tokens")),
                        as_float(event_ai_usage.get("output_usd_per_million_tokens")),
                        as_float(event_ai_usage.get("cached_input_usd_per_million_tokens")),
                        as_float(event_ai_usage.get("estimated_cost_usd")) or 0.0,
                        as_int(request_stats.get("message_count")),
                        as_int(request_stats.get("message_content_chars")),
                        as_int(request_stats.get("message_json_chars")),
                        as_int(request_stats.get("tool_count")),
                        as_int(request_stats.get("tool_schema_json_chars")),
                        as_json(payload),
                        imported_at,
                    )
                )

            event_rows.append(
                (
                    source_file,
                    user_id,
                    session_id,
                    player_session_id,
                    is_dev,
                    client_version,
                    as_int(event.get("event_game_day")) or game_day,
                    event_index,
                    imported_at,
                    event_type,
                    as_int(event.get("event_game_minutes")),
                    event.get("timestamp"),
                    actor.get("agent_id"),
                    actor.get("agent_name") or actor.get("agent_id"),
                    1 if actor.get("is_player") else 0,
                    island_level,
                    as_float(event.get("duration_minutes")),
                    as_float(event.get("energy_cost")),
                    as_float(event.get("meowu_output")),
                    as_int(payload.get("mounted_cats_count")),
                    rod.get("rod_id") or payload.get("rod_id"),
                    rod.get("rod_name") or payload.get("rod_name"),
                    fish.get("fish_id"),
                    fish.get("fish_name"),
                    fish.get("rarity"),
                    as_float(fish.get("price")),
                    as_float(size.get("size_cm")),
                    size.get("size_label"),
                    payload.get("seed_id"),
                    payload.get("seed_name"),
                    payload.get("crop_id"),
                    payload.get("crop_name"),
                    payload.get("crop_rarity"),
                    as_float(payload.get("crop_price")),
                    payload.get("bug_id"),
                    payload.get("bug_name"),
                    payload.get("bug_rarity"),
                    as_float(payload.get("bug_price")),
                    payload.get("recipe_id"),
                    payload.get("recipe_name"),
                    payload.get("building_id"),
                    payload.get("building_name"),
                    payload.get("building_sub_type"),
                    payload.get("item_id"),
                    payload.get("item_name"),
                    as_float(payload.get("money_spent")),
                    as_float(payload.get("earned_money")),
                    payload.get("adopted_cat_id"),
                    payload.get("adopted_cat_name"),
                    payload.get("adopt_source"),
                    sample.get("region") or sample.get("country") or country,
                    as_json(meta),
                    as_json(payload),
                )
            )

    session_ai_usage = select_session_ai_usage(session_meta, ai_event_usages)
    ai_pricing = ai_pricing_payload(session_ai_usage, pricing)
    ai_token_record_count = len(ai_event_usages)
    if not ai_response_count and ai_token_record_count:
        ai_response_count = ai_token_record_count

    conn.execute(
        """
        INSERT INTO gameplay_sessions (
            source_file, user_id, session_id, player_session_id, is_dev, client_version, nickname, country,
            real_time_started_iso, real_time_ended_iso, imported_at,
            game_duration_sec, game_day_start, game_day_end, game_days_total,
            island_level_max, money_start, money_end, money_total_earned,
            money_total_spent, money_net_delta, money_reconciliation_ok,
            pluma_luoqiu_total, new_player_task_progress_json, session_meta_json,
            ai_usage_source, ai_request_count, ai_response_count, ai_token_record_count,
            ai_input_tokens, ai_output_tokens, ai_total_tokens, ai_cached_input_tokens,
            ai_cache_read_input_tokens, ai_cache_creation_input_tokens,
            ai_billable_uncached_input_tokens, ai_billable_cached_input_tokens,
            ai_cache_hit_ratio, ai_estimated_cost_usd, ai_archive_total_consumed_tokens,
            ai_models, ai_pricing_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_file,
            user_id,
            session_id,
            player_session_id,
            is_dev,
            client_version,
            nickname,
            country,
            session_meta.get("real_time_started_iso"),
            session_meta.get("real_time_ended_iso"),
            imported_at,
            as_float(session_meta.get("game_duration_sec")) or 0.0,
            as_int(session_meta.get("game_day_start")),
            as_int(session_meta.get("game_day_end")),
            as_int(session_meta.get("game_days_total")),
            max(island_level_meta, island_level_events),
            as_int(session_meta.get("money_start")),
            as_int(session_meta.get("money_end")),
            as_int(session_meta.get("money_total_earned")),
            as_int(session_meta.get("money_total_spent")),
            as_int(session_meta.get("money_net_delta")),
            as_int(session_meta.get("money_reconciliation_ok")),
            as_int(session_meta.get("pluma_luoqiu_total")),
            as_json(session_meta.get("new_player_task_progress")),
            as_json(session_meta),
            session_ai_usage.get("source") or "",
            ai_request_count,
            ai_response_count,
            ai_token_record_count,
            int0(session_ai_usage.get("input_tokens")),
            int0(session_ai_usage.get("output_tokens")),
            int0(session_ai_usage.get("total_tokens")),
            int0(session_ai_usage.get("cached_input_tokens")),
            int0(session_ai_usage.get("cache_read_input_tokens")),
            int0(session_ai_usage.get("cache_creation_input_tokens")),
            int0(session_ai_usage.get("billable_uncached_input_tokens")),
            int0(session_ai_usage.get("billable_cached_input_tokens")),
            as_float(session_ai_usage.get("cache_hit_ratio")) or 0.0,
            as_float(session_ai_usage.get("estimated_cost_usd")) or 0.0,
            as_int(session_ai_usage.get("archive_total_consumed_tokens")),
            ",".join(sorted(ai_models)),
            as_json(ai_pricing),
        ),
    )

    conn.executemany(
        """
        INSERT INTO gameplay_days (
            source_file, user_id, session_id, player_session_id, is_dev, client_version, game_day, imported_at,
            day_end_completed, energy_remaining_end, energy_total_end,
            cats_count, cats_json, day_meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        day_rows,
    )
    conn.executemany(
        """
        INSERT INTO gameplay_events (
            source_file, user_id, session_id, player_session_id, is_dev, client_version, game_day, event_index, imported_at,
            event_type, event_game_minutes, event_real_time_iso, actor_id, actor_name,
            actor_is_player, island_level, duration_minutes, energy_cost, meowu_output,
            mounted_cats_count, rod_id, rod_name, fish_id, fish_name, fish_rarity,
            fish_price, fish_size_cm, fish_size_label, seed_id, seed_name, crop_id,
            crop_name, crop_rarity, crop_price, bug_id, bug_name, bug_rarity,
            bug_price, recipe_id, recipe_name, building_id, building_name,
            building_sub_type, item_id, item_name, money_spent, earned_money,
            adopted_cat_id, adopted_cat_name, adopt_source, region, meta_json, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        event_rows,
    )
    conn.executemany(
        """
        INSERT INTO gameplay_ai_calls (
            source_file, user_id, session_id, player_session_id, event_index, is_dev, client_version,
            game_day, event_game_minutes, event_real_time_iso, actor_id, actor_name,
            model, mode, tag, toolset_version, prompt_cache_key,
            input_tokens, output_tokens, total_tokens, cached_input_tokens,
            cache_read_input_tokens, cache_creation_input_tokens,
            billable_uncached_input_tokens, billable_cached_input_tokens,
            cache_hit_ratio, input_usd_per_million_tokens, output_usd_per_million_tokens,
            cached_input_usd_per_million_tokens, estimated_cost_usd,
            request_message_count, message_content_chars, message_json_chars,
            tool_count, tool_schema_json_chars, payload_json, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ai_call_rows,
    )
    return 1


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    ensure_column(conn, "gameplay_sessions", "player_session_id", "TEXT")
    ensure_column(conn, "gameplay_sessions", "is_dev", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_sessions", "client_version", "TEXT")
    ensure_column(conn, "gameplay_sessions", "ai_usage_source", "TEXT")
    ensure_column(conn, "gameplay_sessions", "ai_request_count", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_sessions", "ai_response_count", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_sessions", "ai_token_record_count", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_sessions", "ai_input_tokens", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_sessions", "ai_output_tokens", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_sessions", "ai_total_tokens", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_sessions", "ai_cached_input_tokens", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_sessions", "ai_cache_read_input_tokens", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_sessions", "ai_cache_creation_input_tokens", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_sessions", "ai_billable_uncached_input_tokens", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_sessions", "ai_billable_cached_input_tokens", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_sessions", "ai_cache_hit_ratio", "REAL DEFAULT 0")
    ensure_column(conn, "gameplay_sessions", "ai_estimated_cost_usd", "REAL DEFAULT 0")
    ensure_column(conn, "gameplay_sessions", "ai_archive_total_consumed_tokens", "INTEGER")
    ensure_column(conn, "gameplay_sessions", "ai_models", "TEXT")
    ensure_column(conn, "gameplay_sessions", "ai_pricing_json", "TEXT")
    ensure_column(conn, "gameplay_days", "player_session_id", "TEXT")
    ensure_column(conn, "gameplay_days", "is_dev", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_days", "client_version", "TEXT")
    ensure_column(conn, "gameplay_events", "player_session_id", "TEXT")
    ensure_column(conn, "gameplay_events", "is_dev", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_events", "client_version", "TEXT")
    ensure_column(conn, "gameplay_ai_calls", "player_session_id", "TEXT")
    ensure_column(conn, "gameplay_telemetry_ingest", "ingest_id", "TEXT")
    ensure_column(conn, "gameplay_telemetry_ingest", "endpoint", "TEXT")
    ensure_column(conn, "gameplay_telemetry_ingest", "event_type", "TEXT")
    ensure_column(conn, "gameplay_telemetry_ingest", "received_at", "TEXT")
    ensure_column(conn, "gameplay_telemetry_ingest", "user_id", "TEXT")
    ensure_column(conn, "gameplay_telemetry_ingest", "session_id", "TEXT")
    ensure_column(conn, "gameplay_telemetry_ingest", "player_session_id", "TEXT")
    ensure_column(conn, "gameplay_telemetry_ingest", "player_id", "TEXT")
    ensure_column(conn, "gameplay_telemetry_ingest", "client_version", "TEXT")
    ensure_column(conn, "gameplay_telemetry_ingest", "outbox_id", "TEXT")
    ensure_column(conn, "gameplay_telemetry_ingest", "payload_size_bytes", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_telemetry_ingest", "import_status", "TEXT DEFAULT 'pending'")
    ensure_column(conn, "gameplay_telemetry_ingest", "import_error", "TEXT")
    ensure_column(conn, "gameplay_telemetry_ingest", "sample_count", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_telemetry_ingest", "session_count", "INTEGER DEFAULT 0")
    ensure_column(conn, "gameplay_telemetry_ingest", "imported_at", "TEXT")
    ensure_column(conn, "gameplay_telemetry_ingest", "raw_json", "TEXT")

    conn.execute(
        """
        UPDATE gameplay_sessions
        SET player_session_id = COALESCE(
            NULLIF(
                CASE
                    WHEN json_valid(session_meta_json)
                    THEN json_extract(session_meta_json, '$.player_session_id')
                END,
                ''
            ),
            player_session_id,
            ''
        )
        WHERE player_session_id IS NULL OR player_session_id = ''
        """
    )
    conn.execute(
        """
        UPDATE gameplay_sessions
        SET client_version = COALESCE(
            NULLIF(
                CASE
                    WHEN json_valid(session_meta_json)
                    THEN json_extract(session_meta_json, '$.client_version')
                END,
                ''
            ),
            client_version,
            ''
        )
        WHERE client_version IS NULL OR client_version = ''
        """
    )
    conn.execute(
        """
        UPDATE gameplay_days
        SET player_session_id = COALESCE(
            (
                SELECT NULLIF(s.player_session_id, '')
                FROM gameplay_sessions s
                WHERE s.source_file = gameplay_days.source_file
                  AND s.user_id = gameplay_days.user_id
                  AND s.session_id = gameplay_days.session_id
                LIMIT 1
            ),
            player_session_id,
            ''
        )
        WHERE player_session_id IS NULL OR player_session_id = ''
        """
    )
    conn.execute(
        """
        UPDATE gameplay_days
        SET client_version = COALESCE(
            (
                SELECT NULLIF(s.client_version, '')
                FROM gameplay_sessions s
                WHERE s.source_file = gameplay_days.source_file
                  AND s.user_id = gameplay_days.user_id
                  AND s.session_id = gameplay_days.session_id
                LIMIT 1
            ),
            client_version,
            ''
        )
        WHERE client_version IS NULL OR client_version = ''
        """
    )
    conn.execute(
        """
        UPDATE gameplay_events
        SET player_session_id = COALESCE(
            (
                SELECT NULLIF(s.player_session_id, '')
                FROM gameplay_sessions s
                WHERE s.source_file = gameplay_events.source_file
                  AND s.user_id = gameplay_events.user_id
                  AND s.session_id = gameplay_events.session_id
                LIMIT 1
            ),
            player_session_id,
            ''
        )
        WHERE player_session_id IS NULL OR player_session_id = ''
        """
    )
    conn.execute(
        """
        UPDATE gameplay_events
        SET client_version = COALESCE(
            (
                SELECT NULLIF(s.client_version, '')
                FROM gameplay_sessions s
                WHERE s.source_file = gameplay_events.source_file
                  AND s.user_id = gameplay_events.user_id
                  AND s.session_id = gameplay_events.session_id
                LIMIT 1
            ),
            client_version,
            ''
        )
        WHERE client_version IS NULL OR client_version = ''
        """
    )
    conn.execute(
        """
        UPDATE gameplay_ai_calls
        SET player_session_id = COALESCE(
            (
                SELECT NULLIF(s.player_session_id, '')
                FROM gameplay_sessions s
                WHERE s.source_file = gameplay_ai_calls.source_file
                  AND s.user_id = gameplay_ai_calls.user_id
                  AND s.session_id = gameplay_ai_calls.session_id
                LIMIT 1
            ),
            player_session_id,
            ''
        )
        WHERE player_session_id IS NULL OR player_session_id = ''
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gameplay_sessions_client_version ON gameplay_sessions(client_version)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gameplay_sessions_player_session ON gameplay_sessions(user_id, player_session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gameplay_days_client_version ON gameplay_days(client_version)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gameplay_events_client_version ON gameplay_events(client_version)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gameplay_sessions_ai_total_tokens ON gameplay_sessions(ai_total_tokens)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gameplay_sessions_ai_cost ON gameplay_sessions(ai_estimated_cost_usd)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gameplay_telemetry_ingest_status ON gameplay_telemetry_ingest(import_status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gameplay_telemetry_ingest_player_session ON gameplay_telemetry_ingest(user_id, player_session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gameplay_telemetry_ingest_received ON gameplay_telemetry_ingest(received_at)"
    )

    conn.executescript(VIEW_SQL)


def has_imported_source(conn: sqlite3.Connection, source_file: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM gameplay_sessions WHERE source_file = ? LIMIT 1",
        (source_file,),
    ).fetchone()
    return row is not None


def import_file(conn: sqlite3.Connection, path: Path) -> tuple[int, int]:
    source_file = str(path.resolve())
    imported_at = now_iso()
    is_ingest_file = False
    payload: Any
    if path.name.startswith("session-") and path.suffix == ".jsonl":
        payload = json.loads(path.read_text(encoding="utf-8"))
        sample = sample_from_session_record(payload)
        samples = [sample] if sample else []
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        is_ingest_file = is_gameplay_telemetry_ingest_payload(payload)
        if is_ingest_file:
            upsert_ingest_record(
                conn,
                source_file=source_file,
                payload=payload,
                import_status="pending",
                imported_at=imported_at,
            )
        samples = list(iter_samples(payload))

    if not samples:
        logger.debug("Skip %s: no gameplay telemetry samples found", path)
        if is_ingest_file:
            upsert_ingest_record(
                conn,
                source_file=source_file,
                payload=payload,
                import_status="skipped",
                import_error="no gameplay telemetry samples found",
                imported_at=imported_at,
            )
        return 0, 0

    conn.execute("DELETE FROM gameplay_ai_calls WHERE source_file = ?", (source_file,))
    conn.execute("DELETE FROM gameplay_events WHERE source_file = ?", (source_file,))
    conn.execute("DELETE FROM gameplay_days WHERE source_file = ?", (source_file,))
    conn.execute("DELETE FROM gameplay_sessions WHERE source_file = ?", (source_file,))

    session_count = 0
    for sample in samples:
        session_count += import_sample(conn, source_file=source_file, sample=sample, imported_at=imported_at)
    if is_ingest_file:
        upsert_ingest_record(
            conn,
            source_file=source_file,
            payload=payload,
            import_status="imported" if session_count > 0 else "skipped",
            import_error="" if session_count > 0 else "no gameplay sessions imported",
            sample_count=len(samples),
            session_count=session_count,
            imported_at=imported_at,
        )
    return session_count, len(samples)


def run_import_once() -> int:
    TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = ImportState.load(STATE_PATH)
    imported_files = 0
    imported_sessions = 0
    scanned_files = 0
    failed_files = 0
    force_reimport = state.schema_version < STATE_SCHEMA_VERSION
    with sqlite3.connect(DB_PATH) as conn:
        ensure_schema(conn)
        files = discover_json_files(TELEMETRY_DIR) + discover_session_files(OUTPUT_DIR)
        if not files:
            logger.info("No gameplay telemetry sources found under %s or %s", TELEMETRY_DIR, OUTPUT_DIR)
            conn.commit()
            state.schema_version = STATE_SCHEMA_VERSION
            state.save(STATE_PATH)
            return 0
        for path in files:
            mtime_ns = path.stat().st_mtime_ns
            key = str(path.resolve())
            if not force_reimport and state.files.get(key) == mtime_ns:
                continue
            scanned_files += 1
            logger.debug("Importing gameplay telemetry from %s", path)
            savepoint = f"import_file_{scanned_files}"
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                session_count, sample_count = import_file(conn, path)
            except Exception:
                conn.execute(f"ROLLBACK TO {savepoint}")
                conn.execute(f"RELEASE {savepoint}")
                logger.exception("Failed to import %s", path)
                record_ingest_import_failure(conn, path, str(exc))
                failed_files += 1
                continue
            conn.execute(f"RELEASE {savepoint}")
            state.files[key] = mtime_ns
            imported_files += 1
            imported_sessions += session_count
            if session_count > 0:
                logger.debug(
                    "Imported %s sessions from %s samples in %s",
                    session_count,
                    sample_count,
                    path.name,
                )
        conn.commit()

    if failed_files == 0:
        state.schema_version = STATE_SCHEMA_VERSION
    state.save(STATE_PATH)
    logger.info(
        "Gameplay import finished, %s file(s) scanned, %s file(s) updated, %s session(s) imported",
        scanned_files,
        imported_files,
        imported_sessions,
    )
    return imported_files


def main() -> None:
    logger.info("Gameplay importer started")
    logger.info("DB_PATH=%s", DB_PATH)
    logger.info("GAMEPLAY_TELEMETRY_DIR=%s", TELEMETRY_DIR)
    logger.info("OUTPUT_DIR=%s", OUTPUT_DIR)
    logger.info("STATE_PATH=%s", STATE_PATH)

    loop_mode = "--loop" in os.sys.argv
    if not loop_mode:
        run_import_once()
        return

    while True:
        try:
            run_import_once()
        except Exception:
            logger.exception("Gameplay import loop failed")
        time.sleep(IMPORT_INTERVAL_SEC)


if __name__ == "__main__":
    main()
