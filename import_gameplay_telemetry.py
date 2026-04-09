#!/usr/bin/env python3
"""
Import gameplay telemetry JSON into SQLite for Grafana analytics.

The importer scans a directory of JSON files, flattens telemetry samples into
session/day/event tables, and keeps the import idempotent by replacing data for
each source file on re-import.
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
STATE_PATH = Path(os.getenv("GAMEPLAY_IMPORTER_STATE_PATH", "/app/data/gameplay_importer_state.json"))
IMPORT_INTERVAL_SEC = int(os.getenv("GAMEPLAY_IMPORT_INTERVAL_SEC", "300"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

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
    PRIMARY KEY (source_file, user_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_gameplay_sessions_user ON gameplay_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_gameplay_sessions_started ON gameplay_sessions(real_time_started_iso);

CREATE TABLE IF NOT EXISTS gameplay_days (
    source_file TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
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

CREATE VIEW IF NOT EXISTS gameplay_player_summary AS
WITH player_sessions AS (
    SELECT
        s.user_id,
        COALESCE(NULLIF(MAX(s.nickname), ''), s.user_id) AS nickname,
        MIN(date(s.real_time_started_iso)) AS first_login_date,
        MIN(s.real_time_started_iso) AS first_login_iso,
        MAX(s.real_time_started_iso) AS latest_session_started_iso,
        SUM(COALESCE(s.game_duration_sec, 0)) AS total_play_duration_sec,
        MAX(COALESCE(s.game_days_total, s.game_day_end, 0)) AS play_days_total,
        MAX(COALESCE(s.island_level_max, 0)) AS island_level_max
    FROM gameplay_sessions s
    GROUP BY s.user_id
),
latest_session AS (
    SELECT
        user_id,
        money_end,
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
    ps.nickname,
    ps.first_login_date,
    ps.first_login_iso,
    ps.latest_session_started_iso,
    ROUND(ps.total_play_duration_sec, 2) AS total_play_duration_sec,
    ps.play_days_total,
    ps.island_level_max,
    COALESCE(ls.money_end, 0) AS total_money,
    COALESCE(ls.country, '') AS country,
    COALESCE(ld.cats_count, 0) AS cats_count
FROM player_sessions ps
LEFT JOIN latest_session ls
    ON ls.user_id = ps.user_id AND ls.rn = 1
LEFT JOIN latest_day ld
    ON ld.user_id = ps.user_id AND ld.rn = 1;
"""


@dataclass
class ImportState:
    files: dict[str, int]

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
        normalized = {}
        for key, value in files.items():
            try:
                normalized[str(key)] = int(value)
            except Exception:
                continue
        return cls(files=normalized)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
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


def discover_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.json") if path.is_file())


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
    country = infer_country(sample, session_meta)
    session_id = str(session_meta.get("session_id") or f"{user_id}:{source_file}")
    days = telemetry.get("days") or {}
    if not isinstance(days, dict):
        days = {}

    island_level_meta = as_int(session_meta.get("island_level_max")) or 0
    island_level_events = 0
    event_rows = []
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

            event_rows.append(
                (
                    source_file,
                    user_id,
                    session_id,
                    as_int(event.get("event_game_day")) or game_day,
                    event_index,
                    imported_at,
                    event.get("event_type"),
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

    conn.execute(
        """
        INSERT INTO gameplay_sessions (
            source_file, user_id, session_id, nickname, country,
            real_time_started_iso, real_time_ended_iso, imported_at,
            game_duration_sec, game_day_start, game_day_end, game_days_total,
            island_level_max, money_start, money_end, money_total_earned,
            money_total_spent, money_net_delta, money_reconciliation_ok,
            pluma_luoqiu_total, new_player_task_progress_json, session_meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_file,
            user_id,
            session_id,
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
        ),
    )

    conn.executemany(
        """
        INSERT INTO gameplay_days (
            source_file, user_id, session_id, game_day, imported_at,
            day_end_completed, energy_remaining_end, energy_total_end,
            cats_count, cats_json, day_meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        day_rows,
    )
    conn.executemany(
        """
        INSERT INTO gameplay_events (
            source_file, user_id, session_id, game_day, event_index, imported_at,
            event_type, event_game_minutes, event_real_time_iso, actor_id, actor_name,
            actor_is_player, island_level, duration_minutes, energy_cost, meowu_output,
            mounted_cats_count, rod_id, rod_name, fish_id, fish_name, fish_rarity,
            fish_price, fish_size_cm, fish_size_label, seed_id, seed_name, crop_id,
            crop_name, crop_rarity, crop_price, bug_id, bug_name, bug_rarity,
            bug_price, recipe_id, recipe_name, building_id, building_name,
            building_sub_type, item_id, item_name, money_spent, earned_money,
            adopted_cat_id, adopted_cat_name, adopt_source, region, meta_json, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        event_rows,
    )
    return 1


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def import_file(conn: sqlite3.Connection, path: Path) -> tuple[int, int]:
    source_file = str(path.resolve())
    imported_at = now_iso()
    payload = json.loads(path.read_text(encoding="utf-8"))
    samples = list(iter_samples(payload))
    if not samples:
        logger.info("Skip %s: no gameplay telemetry samples found", path)
        return 0, 0

    conn.execute("DELETE FROM gameplay_events WHERE source_file = ?", (source_file,))
    conn.execute("DELETE FROM gameplay_days WHERE source_file = ?", (source_file,))
    conn.execute("DELETE FROM gameplay_sessions WHERE source_file = ?", (source_file,))

    session_count = 0
    for sample in samples:
        session_count += import_sample(conn, source_file=source_file, sample=sample, imported_at=imported_at)
    return session_count, len(samples)


def run_import_once() -> int:
    TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = ImportState.load(STATE_PATH)
    files = discover_files(TELEMETRY_DIR)

    if not files:
        logger.info("No telemetry files found under %s", TELEMETRY_DIR)
        return 0

    imported_files = 0
    with sqlite3.connect(DB_PATH) as conn:
        ensure_schema(conn)
        for path in files:
            mtime_ns = path.stat().st_mtime_ns
            key = str(path.resolve())
            if state.files.get(key) == mtime_ns:
                continue
            logger.info("Importing gameplay telemetry from %s", path)
            try:
                session_count, sample_count = import_file(conn, path)
            except Exception:
                logger.exception("Failed to import %s", path)
                continue
            state.files[key] = mtime_ns
            imported_files += 1
            logger.info(
                "Imported %s sessions from %s samples in %s",
                session_count,
                sample_count,
                path.name,
            )
        conn.commit()

    state.save(STATE_PATH)
    logger.info("Gameplay import finished, %s file(s) updated", imported_files)
    return imported_files


def main() -> None:
    logger.info("Gameplay importer started")
    logger.info("DB_PATH=%s", DB_PATH)
    logger.info("GAMEPLAY_TELEMETRY_DIR=%s", TELEMETRY_DIR)
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
