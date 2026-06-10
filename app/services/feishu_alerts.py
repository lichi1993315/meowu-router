import asyncio
import datetime
import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

import httpx

from app.core.logging import log


_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
_DOCX_URL = "https://open.feishu.cn/open-apis/docx/v1/documents"
_DRIVE_PERMISSION_URL = "https://open.feishu.cn/open-apis/drive/v1/permissions"
_DEFAULT_TIMEOUT = 20.0
_DEFAULT_MAX_CHARS = 3500
_DOC_BLOCK_CHARS = 3000
_DOC_BLOCKS_PER_REQUEST = 50


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _is_enabled() -> bool:
    value = os.getenv("FEISHU_ERROR_LOG_ALERTS", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _is_logoff_report_enabled() -> bool:
    value = os.getenv("FEISHU_LOGOFF_REPORTS", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _missing_config() -> list[str]:
    required = ("FEISHU_BOT_API_KEY", "FEISHU_BOT_API_SECRET", "FEISHU_CHAT_ID")
    return [name for name in required if not _env(name)]


def _missing_logoff_report_config() -> list[str]:
    missing = _missing_config()
    if not _env("FEISHU_ADMIN_ID"):
        missing.append("FEISHU_ADMIN_ID")
    return missing


def _clean_text(value: Any, *, max_chars: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 20].rstrip() + "\n...[truncated]"


def _session_meta_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    telemetry = payload.get("gameplay_telemetry")
    if not isinstance(telemetry, dict):
        return {}
    session_meta = telemetry.get("session_meta")
    return session_meta if isinstance(session_meta, dict) else {}


def _header_value(headers: dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name:
            return str(value).strip()
    return ""


def _client_version_values(payload: dict[str, Any], headers: dict[str, str]) -> list[str]:
    session_meta = _session_meta_from_payload(payload)
    values = (
        _header_value(headers, "x-client-version"),
        payload.get("client_version"),
        session_meta.get("client_version"),
    )
    return [str(value).strip() for value in values if str(value).strip()]


def _client_version_from_payload(payload: dict[str, Any], headers: dict[str, str]) -> str:
    versions = _client_version_values(payload, headers)
    return versions[0] if versions else ""


def _is_unity_dev_payload(payload: dict[str, Any], headers: dict[str, str]) -> bool:
    return any(version.lower() == "unity-dev" for version in _client_version_values(payload, headers))


def _format_duration(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _count_telemetry_events(telemetry: dict[str, Any]) -> int:
    days = telemetry.get("days")
    if not isinstance(days, dict):
        return 0
    total = 0
    for day in days.values():
        if not isinstance(day, dict):
            continue
        events = day.get("events")
        if isinstance(events, list):
            total += len(events)
    return total


def _iter_telemetry_events(telemetry: dict[str, Any]):
    days = telemetry.get("days")
    if not isinstance(days, dict):
        return
    for day in days.values():
        if not isinstance(day, dict):
            continue
        events = day.get("events")
        if not isinstance(events, list):
            continue
        for event in events:
            if isinstance(event, dict):
                yield event


def _to_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_count(value: Any) -> str:
    return f"{_to_int(value):,}"


def _fmt_usd(value: Any) -> str:
    amount = _to_float(value)
    if amount is None:
        return ""
    if amount < 0.01:
        return f"${amount:.4f}"
    return f"${amount:.2f}"


def _event_type_counts(telemetry: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in _iter_telemetry_events(telemetry):
        event_type = event.get("event_type") or event.get("type") or "unknown"
        counts[str(event_type)] = counts.get(str(event_type), 0) + 1
    return counts


def _ai_activity_summary(telemetry: dict[str, Any]) -> dict[str, Any]:
    counts = _event_type_counts(telemetry)
    models: set[str] = set()
    for event in _iter_telemetry_events(telemetry):
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("model"):
            models.add(str(payload["model"]))

    return {
        "request_events": counts.get("cat_agent_request", 0),
        "response_events": counts.get("cat_agent_response", 0),
        "decision_events": counts.get("cat_decision_made", 0),
        "models": sorted(models),
        "top_event_types": sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10],
    }


def _calculate_token_cost(usage: dict[str, Any]) -> float | None:
    input_rate = _to_float(usage.get("input_usd_per_million_tokens"))
    output_rate = _to_float(usage.get("output_usd_per_million_tokens"))
    cached_rate = _to_float(usage.get("cached_input_usd_per_million_tokens"))
    if input_rate is None or output_rate is None:
        return None

    cached_input = _to_int(usage.get("billable_cached_input_tokens"))
    uncached_input = _to_int(usage.get("billable_uncached_input_tokens"))
    if not uncached_input and not cached_input:
        cached_input = _to_int(usage.get("cached_input_tokens"))
        uncached_input = max(0, _to_int(usage.get("input_tokens")) - cached_input)

    cached_cost = cached_input * cached_rate if cached_rate is not None else 0
    return (
        (uncached_input * input_rate)
        + cached_cost
        + (_to_int(usage.get("output_tokens")) * output_rate)
    ) / 1_000_000


def _normalize_ai_token_usage(raw: dict[str, Any], *, source: str) -> dict[str, Any]:
    cached_input = (
        _to_int(raw.get("session_cached_input_tokens"))
        or _to_int(raw.get("cached_input_tokens"))
        or _to_int(raw.get("cached_tokens"))
        or _to_int(raw.get("cache_read_input_tokens"))
    )
    usage = {
        "source": source,
        "archive_total_consumed_tokens": _to_int(raw.get("archive_total_consumed_tokens")),
        "input_tokens": (
            _to_int(raw.get("session_input_tokens"))
            or _to_int(raw.get("input_tokens"))
            or _to_int(raw.get("prompt_tokens"))
        ),
        "output_tokens": (
            _to_int(raw.get("session_output_tokens"))
            or _to_int(raw.get("output_tokens"))
            or _to_int(raw.get("completion_tokens"))
        ),
        "total_tokens": (
            _to_int(raw.get("session_total_tokens"))
            or _to_int(raw.get("total_tokens"))
        ),
        "cached_input_tokens": cached_input,
        "cache_read_input_tokens": (
            _to_int(raw.get("session_cache_read_input_tokens"))
            or _to_int(raw.get("cache_read_input_tokens"))
            or cached_input
        ),
        "cache_creation_input_tokens": (
            _to_int(raw.get("session_cache_creation_input_tokens"))
            or _to_int(raw.get("cache_creation_input_tokens"))
        ),
        "billable_uncached_input_tokens": (
            _to_int(raw.get("session_billable_uncached_input_tokens"))
            or _to_int(raw.get("billable_uncached_input_tokens"))
        ),
        "billable_cached_input_tokens": (
            _to_int(raw.get("session_billable_cached_input_tokens"))
            or _to_int(raw.get("billable_cached_input_tokens"))
        ),
        "input_usd_per_million_tokens": _to_float(raw.get("input_usd_per_million_tokens")),
        "output_usd_per_million_tokens": _to_float(raw.get("output_usd_per_million_tokens")),
        "cached_input_usd_per_million_tokens": _to_float(raw.get("cached_input_usd_per_million_tokens")),
    }
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
        usage["cached_input_tokens"] / usage["input_tokens"]
        if usage["input_tokens"]
        else 0
    )
    usage["estimated_cost_usd"] = _calculate_token_cost(usage)
    return usage


def _event_token_usage(event: dict[str, Any]) -> dict[str, Any] | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None

    raw = payload.get("response_stats")
    if not isinstance(raw, dict):
        raw = payload

    usage = _normalize_ai_token_usage(raw, source="event_payloads")
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
    usage["estimated_cost_usd"] = _calculate_token_cost(usage)

    if not (usage["input_tokens"] or usage["output_tokens"] or usage["total_tokens"]):
        return None
    if payload.get("model"):
        usage["model"] = str(payload["model"])
    return usage


def _aggregate_event_ai_token_usage(telemetry: dict[str, Any]) -> dict[str, Any] | None:
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
        "token_usage_events": 0,
        "models": [],
        "estimated_cost_usd": None,
    }
    models: set[str] = set()
    estimated_cost = 0.0
    has_cost = False

    for event in _iter_telemetry_events(telemetry):
        usage = _event_token_usage(event)
        if not usage:
            continue
        aggregate["token_usage_events"] += 1
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "billable_uncached_input_tokens",
            "billable_cached_input_tokens",
        ):
            aggregate[key] += _to_int(usage.get(key))
        if usage.get("model"):
            models.add(str(usage["model"]))
        if usage.get("estimated_cost_usd") is not None:
            estimated_cost += float(usage["estimated_cost_usd"])
            has_cost = True

    if not aggregate["token_usage_events"]:
        return None
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
        aggregate["cached_input_tokens"] / aggregate["input_tokens"]
        if aggregate["input_tokens"]
        else 0
    )
    aggregate["models"] = sorted(models)
    if has_cost:
        aggregate["estimated_cost_usd"] = estimated_cost
    return aggregate


def _extract_ai_token_usage(telemetry: dict[str, Any]) -> dict[str, Any] | None:
    session_meta = telemetry.get("session_meta")
    if not isinstance(session_meta, dict):
        session_meta = {}

    event_usage = _aggregate_event_ai_token_usage(telemetry)
    raw = session_meta.get("ai_token_usage")
    if isinstance(raw, dict):
        usage = _normalize_ai_token_usage(raw, source="session_meta.ai_token_usage")
        if usage["input_tokens"] or usage["output_tokens"] or usage["total_tokens"]:
            if event_usage:
                usage["token_usage_events"] = event_usage.get("token_usage_events", 0)
                usage["models"] = event_usage.get("models", [])
            return usage

    return event_usage


def _format_ai_token_usage_lines(token_usage: dict[str, Any] | None) -> list[str]:
    if not token_usage:
        return []

    lines = [
        (
            "ai_tokens: "
            f"input={_fmt_count(token_usage.get('input_tokens'))} "
            f"output={_fmt_count(token_usage.get('output_tokens'))} "
            f"total={_fmt_count(token_usage.get('total_tokens'))} "
            f"cached_input={_fmt_count(token_usage.get('cached_input_tokens'))}"
        ),
        (
            "ai_cache: "
            f"read={_fmt_count(token_usage.get('cache_read_input_tokens'))} "
            f"created={_fmt_count(token_usage.get('cache_creation_input_tokens'))} "
            f"hit={float(token_usage.get('cache_hit_ratio') or 0):.1%}"
        ),
        (
            "ai_billable_input: "
            f"uncached={_fmt_count(token_usage.get('billable_uncached_input_tokens'))} "
            f"cached={_fmt_count(token_usage.get('billable_cached_input_tokens'))}"
        ),
    ]
    if token_usage.get("estimated_cost_usd") is not None:
        lines.append(f"ai_cost_est_usd: {_fmt_usd(token_usage.get('estimated_cost_usd'))}")
    if token_usage.get("archive_total_consumed_tokens"):
        lines.append(
            "ai_archive_total_consumed_tokens: "
            f"{_fmt_count(token_usage.get('archive_total_consumed_tokens'))}"
        )
    return lines


def _current_session_aggregate(
    *,
    session_id: str,
    duration: Any,
    days_total: Any,
    game_day_end: Any,
    island_level: Any,
    ai_activity: dict[str, Any],
    token_usage: dict[str, Any] | None,
    session_started_iso: Any,
) -> dict[str, Any]:
    return {
        "source": "current_logoff",
        "session_count": 1 if session_id else 0,
        "game_duration_sec": _to_float(duration) or 0.0,
        "ai_request_count": _to_int(ai_activity.get("request_events")),
        "ai_response_count": _to_int(ai_activity.get("response_events")),
        "ai_token_record_count": _to_int(token_usage.get("token_usage_events") if token_usage else 0),
        "ai_input_tokens": _to_int(token_usage.get("input_tokens") if token_usage else 0),
        "ai_output_tokens": _to_int(token_usage.get("output_tokens") if token_usage else 0),
        "ai_total_tokens": _to_int(token_usage.get("total_tokens") if token_usage else 0),
        "ai_cached_input_tokens": _to_int(token_usage.get("cached_input_tokens") if token_usage else 0),
        "ai_estimated_cost_usd": _to_float(token_usage.get("estimated_cost_usd") if token_usage else None) or 0.0,
        "play_days_total": max(_to_int(days_total), _to_int(game_day_end)),
        "island_level_max": _to_int(island_level),
        "first_session_iso": str(session_started_iso or ""),
        "latest_session_iso": str(session_started_iso or ""),
    }


def _merge_current_session_aggregate(
    aggregate: dict[str, Any],
    current: dict[str, Any],
    *,
    current_session_in_db: bool,
) -> dict[str, Any]:
    merged = dict(aggregate)
    if current_session_in_db:
        merged["source"] = "db"
        return merged

    merged["source"] = "db+current_logoff" if merged.get("session_count") else "current_logoff"
    for key in (
        "session_count",
        "ai_request_count",
        "ai_response_count",
        "ai_token_record_count",
        "ai_input_tokens",
        "ai_output_tokens",
        "ai_total_tokens",
        "ai_cached_input_tokens",
    ):
        merged[key] = _to_int(merged.get(key)) + _to_int(current.get(key))
    merged["game_duration_sec"] = (
        (_to_float(merged.get("game_duration_sec")) or 0.0)
        + (_to_float(current.get("game_duration_sec")) or 0.0)
    )
    merged["ai_estimated_cost_usd"] = (
        (_to_float(merged.get("ai_estimated_cost_usd")) or 0.0)
        + (_to_float(current.get("ai_estimated_cost_usd")) or 0.0)
    )
    merged["play_days_total"] = max(_to_int(merged.get("play_days_total")), _to_int(current.get("play_days_total")))
    merged["island_level_max"] = max(_to_int(merged.get("island_level_max")), _to_int(current.get("island_level_max")))

    current_first = current.get("first_session_iso") or ""
    current_latest = current.get("latest_session_iso") or ""
    existing_first = merged.get("first_session_iso") or current_first
    existing_latest = merged.get("latest_session_iso") or current_latest
    if current_first and (not existing_first or current_first < existing_first):
        existing_first = current_first
    if current_latest and (not existing_latest or current_latest > existing_latest):
        existing_latest = current_latest
    merged["first_session_iso"] = existing_first
    merged["latest_session_iso"] = existing_latest
    return merged


def _query_user_gameplay_aggregate(
    *,
    user_id: str,
    session_id: str,
    current: dict[str, Any],
) -> dict[str, Any] | None:
    if not user_id:
        return current if current.get("session_count") else None

    db_path = _env("DB_PATH")
    if not db_path or not Path(db_path).exists():
        return current if current.get("session_count") else None

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS session_count,
                    COALESCE(SUM(COALESCE(game_duration_sec, 0)), 0) AS game_duration_sec,
                    COALESCE(SUM(COALESCE(ai_request_count, 0)), 0) AS ai_request_count,
                    COALESCE(SUM(COALESCE(ai_response_count, 0)), 0) AS ai_response_count,
                    COALESCE(SUM(COALESCE(ai_token_record_count, 0)), 0) AS ai_token_record_count,
                    COALESCE(SUM(COALESCE(ai_input_tokens, 0)), 0) AS ai_input_tokens,
                    COALESCE(SUM(COALESCE(ai_output_tokens, 0)), 0) AS ai_output_tokens,
                    COALESCE(SUM(COALESCE(ai_total_tokens, 0)), 0) AS ai_total_tokens,
                    COALESCE(SUM(COALESCE(ai_cached_input_tokens, 0)), 0) AS ai_cached_input_tokens,
                    COALESCE(SUM(COALESCE(ai_estimated_cost_usd, 0)), 0) AS ai_estimated_cost_usd,
                    COALESCE(MAX(COALESCE(game_days_total, game_day_end, 0)), 0) AS play_days_total,
                    COALESCE(MAX(COALESCE(island_level_max, 0)), 0) AS island_level_max,
                    MIN(real_time_started_iso) AS first_session_iso,
                    MAX(real_time_started_iso) AS latest_session_iso,
                    MAX(CASE WHEN session_id = ? THEN 1 ELSE 0 END) AS current_session_in_db
                FROM gameplay_sessions
                WHERE user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
    except Exception as exc:
        log(f"[WARNING] Failed to query gameplay aggregate for Feishu logoff report: {exc}")
        return current if current.get("session_count") else None

    if row is None or not row["session_count"]:
        return current if current.get("session_count") else None

    aggregate = {
        "source": "db",
        "session_count": _to_int(row["session_count"]),
        "game_duration_sec": _to_float(row["game_duration_sec"]) or 0.0,
        "ai_request_count": _to_int(row["ai_request_count"]),
        "ai_response_count": _to_int(row["ai_response_count"]),
        "ai_token_record_count": _to_int(row["ai_token_record_count"]),
        "ai_input_tokens": _to_int(row["ai_input_tokens"]),
        "ai_output_tokens": _to_int(row["ai_output_tokens"]),
        "ai_total_tokens": _to_int(row["ai_total_tokens"]),
        "ai_cached_input_tokens": _to_int(row["ai_cached_input_tokens"]),
        "ai_estimated_cost_usd": _to_float(row["ai_estimated_cost_usd"]) or 0.0,
        "play_days_total": _to_int(row["play_days_total"]),
        "island_level_max": _to_int(row["island_level_max"]),
        "first_session_iso": row["first_session_iso"] or "",
        "latest_session_iso": row["latest_session_iso"] or "",
    }
    return _merge_current_session_aggregate(
        aggregate,
        current,
        current_session_in_db=bool(row["current_session_in_db"]),
    )


def _format_user_aggregate_lines(aggregate: dict[str, Any] | None) -> list[str]:
    if not aggregate:
        return []

    input_tokens = _to_int(aggregate.get("ai_input_tokens"))
    cached_input = _to_int(aggregate.get("ai_cached_input_tokens"))
    cache_hit_ratio = cached_input / input_tokens if input_tokens else 0
    duration_sec = _to_float(aggregate.get("game_duration_sec")) or 0.0
    first_session = aggregate.get("first_session_iso") or ""
    latest_session = aggregate.get("latest_session_iso") or ""

    return [
        (
            "user_total_sessions: "
            f"{_fmt_count(aggregate.get('session_count'))} "
            f"(source={aggregate.get('source') or ''})"
        ),
        (
            "user_total_playtime: "
            f"{_format_duration(duration_sec)} "
            f"({_fmt_count(duration_sec)}s)"
        ),
        (
            "user_total_ai_tokens: "
            f"input={_fmt_count(aggregate.get('ai_input_tokens'))} "
            f"output={_fmt_count(aggregate.get('ai_output_tokens'))} "
            f"total={_fmt_count(aggregate.get('ai_total_tokens'))} "
            f"cached_input={_fmt_count(aggregate.get('ai_cached_input_tokens'))}"
        ),
        f"user_total_ai_cost_est_usd: {_fmt_usd(aggregate.get('ai_estimated_cost_usd'))}",
        (
            "user_total_ai_calls: "
            f"request={_fmt_count(aggregate.get('ai_request_count'))} "
            f"response={_fmt_count(aggregate.get('ai_response_count'))} "
            f"token_records={_fmt_count(aggregate.get('ai_token_record_count'))}"
        ),
        f"user_ai_cache_hit: {cache_hit_ratio:.1%}",
        (
            "user_progress_max: "
            f"game_days={_fmt_count(aggregate.get('play_days_total'))} "
            f"island_level={_fmt_count(aggregate.get('island_level_max'))}"
        ),
        f"user_session_window: {first_session} -> {latest_session}",
    ]


def _chunk_text(text: str, *, chunk_size: int = _DOC_BLOCK_CHARS) -> list[str]:
    if not text:
        return [""]
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


def _now_title() -> str:
    try:
        tzinfo = ZoneInfo(os.getenv("REPORT_TIMEZONE", "Asia/Shanghai"))
    except Exception:
        tzinfo = ZoneInfo("Asia/Shanghai")
    return datetime.datetime.now(tzinfo).strftime("%Y-%m-%d %H:%M:%S")


def _doc_url(document_id: str) -> str:
    base_url = os.getenv("FEISHU_DOC_BASE_URL", "https://www.feishu.cn/docx").strip()
    if not base_url:
        return document_id
    if "{document_id}" in base_url:
        return base_url.replace("{document_id}", document_id)
    if "{doc_id}" in base_url:
        return base_url.replace("{doc_id}", document_id)

    parsed = urlparse(base_url)
    if parsed.scheme and parsed.netloc:
        path = parsed.path.rstrip("/")
        if not path:
            path = "/docx"
        elif path != "/docx" and not path.endswith("/docx"):
            path = f"{path}/docx"
        return urlunparse(parsed._replace(path=f"{path}/{document_id}"))

    return f"{base_url.rstrip('/')}/docx/{document_id}"


def _infer_member_type(member_id: str) -> str:
    explicit = os.getenv("FEISHU_ADMIN_MEMBER_TYPE", "").strip()
    if explicit:
        return explicit
    if member_id.startswith("ou_"):
        return "openid"
    if member_id.startswith("oc_"):
        return "openchat"
    return "userid"


def _text_block(content: str) -> dict[str, Any]:
    return {
        "block_type": 2,
        "text": {
            "elements": [
                {
                    "text_run": {
                        "content": content,
                        "text_element_style": {},
                    }
                }
            ],
            "style": {},
        },
    }


def _build_error_log_alert_text(
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    received_at: str,
    decrypted_body_bytes: int,
) -> str:
    user_id = headers.get("x-user-id") or payload.get("user_id") or payload.get("player_id") or ""
    session_id = headers.get("x-session-id") or payload.get("session_id") or ""
    client_version = _client_version_from_payload(payload, headers)
    reason = payload.get("reason") or payload.get("error_reason") or ""
    message = payload.get("message") or payload.get("error") or payload.get("exception") or ""
    message = _clean_text(message, max_chars=2400)

    lines = [
        f"游戏 Error Log 告警 ({_now_title()})",
        "",
        f"received_at: {received_at}",
        f"user_id: {user_id}",
        f"session_id: {session_id}",
        f"client_version: {client_version}",
        f"content_type: {headers.get('content-type', '')}",
        f"x_encrypted: {headers.get('x-encrypted', '')}",
        f"encrypted_content_length: {headers.get('content-length', '')}",
        f"decrypted_body_bytes: {decrypted_body_bytes}",
    ]
    if reason:
        lines.append(f"reason: {_clean_text(reason, max_chars=300)}")
    if payload.get("timestamp"):
        lines.append(f"client_timestamp: {_clean_text(payload.get('timestamp'), max_chars=120)}")
    if message:
        lines.extend(["", "message:", message])

    text = "\n".join(lines)
    max_chars = int(os.getenv("FEISHU_ERROR_LOG_ALERT_MAX_CHARS", str(_DEFAULT_MAX_CHARS)))
    return _clean_text(text, max_chars=max_chars)


async def _get_tenant_access_token(client: httpx.AsyncClient) -> str | None:
    response = await client.post(
        _TOKEN_URL,
        json={
            "app_id": _env("FEISHU_BOT_API_KEY"),
            "app_secret": _env("FEISHU_BOT_API_SECRET"),
        },
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        log(f"[WARNING] Feishu token request failed for error_log alert: {data}")
        return None
    return data.get("tenant_access_token")


async def _send_text_message(client: httpx.AsyncClient, token: str, text: str) -> bool:
    receive_id_type = os.getenv("FEISHU_RECEIVE_ID_TYPE", "chat_id").strip() or "chat_id"
    response = await client.post(
        f"{_MESSAGE_URL}?receive_id_type={receive_id_type}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "receive_id": _env("FEISHU_CHAT_ID"),
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        log(f"[WARNING] Feishu text message send failed: {data}")
        return False
    return True


async def _create_docx(client: httpx.AsyncClient, token: str, title: str) -> str | None:
    response = await client.post(
        _DOCX_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={"title": title},
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        log(f"[WARNING] Feishu docx create failed: {data}")
        return None
    return (data.get("data") or {}).get("document", {}).get("document_id")


async def _append_docx_text(client: httpx.AsyncClient, token: str, document_id: str, text: str) -> bool:
    chunks = _chunk_text(text)
    blocks = [_text_block(chunk) for chunk in chunks]
    url = f"{_DOCX_URL}/{document_id}/blocks/{document_id}/children"

    for start in range(0, len(blocks), _DOC_BLOCKS_PER_REQUEST):
        batch = blocks[start : start + _DOC_BLOCKS_PER_REQUEST]
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"index": -1, "children": batch},
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            log(f"[WARNING] Feishu docx append failed: {data}")
            return False
        if start + _DOC_BLOCKS_PER_REQUEST < len(blocks):
            await asyncio.sleep(0.45)
    return True


async def _grant_docx_full_access(client: httpx.AsyncClient, token: str, document_id: str) -> bool:
    admin_id = _env("FEISHU_ADMIN_ID")
    member_type = _infer_member_type(admin_id)
    response = await client.post(
        f"{_DRIVE_PERMISSION_URL}/{document_id}/members?type=docx&need_notification=false",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "member_type": member_type,
            "member_id": admin_id,
            "perm": "full_access",
        },
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        log(f"[WARNING] Feishu docx permission grant failed: {data}")
        return False
    return True


async def send_error_log_alert(
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    received_at: str,
    decrypted_body_bytes: int,
) -> None:
    if not _is_enabled():
        return

    if _is_unity_dev_payload(payload, headers):
        return

    missing = _missing_config()
    if missing:
        log(f"[WARNING] Feishu error_log alert skipped; missing env: {', '.join(missing)}")
        return

    text = _build_error_log_alert_text(
        payload=payload,
        headers=headers,
        received_at=received_at,
        decrypted_body_bytes=decrypted_body_bytes,
    )
    timeout = float(os.getenv("FEISHU_TIMEOUT", str(_DEFAULT_TIMEOUT)))

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            token = await _get_tenant_access_token(client)
            if not token:
                return

            await _send_text_message(client, token, text)
    except Exception as exc:
        log(f"[WARNING] Feishu error_log alert failed: {exc}")


def _build_logoff_report_summary(
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    received_at: str,
    document_url: str | None = None,
) -> str:
    telemetry = payload.get("gameplay_telemetry")
    if not isinstance(telemetry, dict):
        telemetry = {}
    session_meta = telemetry.get("session_meta")
    if not isinstance(session_meta, dict):
        session_meta = {}

    task_progress = session_meta.get("new_player_task_progress")
    if not isinstance(task_progress, dict):
        task_progress = {}

    user_id = headers.get("x-user-id") or payload.get("user_id") or payload.get("player_id") or ""
    session_id = headers.get("x-session-id") or payload.get("session_id") or session_meta.get("session_id") or ""
    user_profile = payload.get("user_profile")
    username = payload.get("username")
    if not username and isinstance(user_profile, dict):
        username = user_profile.get("username") or user_profile.get("nickname")
    client_version = _client_version_from_payload(payload, headers)
    duration = (
        payload.get("session_duration_sec")
        or session_meta.get("game_duration_sec")
        or ""
    )
    game_day_start = session_meta.get("game_day_start")
    game_day_end = session_meta.get("game_day_end")
    days_total = session_meta.get("game_days_total") or len(telemetry.get("days") or {})
    events_total = _count_telemetry_events(telemetry)
    ai_activity = _ai_activity_summary(telemetry)
    token_usage = _extract_ai_token_usage(telemetry)
    current_aggregate = _current_session_aggregate(
        session_id=str(session_id or ""),
        duration=duration,
        days_total=days_total,
        game_day_end=game_day_end,
        island_level=payload.get("island_level") or session_meta.get("island_level_max"),
        ai_activity=ai_activity,
        token_usage=token_usage,
        session_started_iso=session_meta.get("real_time_started_iso"),
    )
    user_aggregate = _query_user_gameplay_aggregate(
        user_id=str(user_id or ""),
        session_id=str(session_id or ""),
        current=current_aggregate,
    )
    tasks_completed = payload.get("tasks_completed") or task_progress.get("tasks_completed")
    tasks_total = payload.get("tasks_total") or task_progress.get("tasks_total")
    money_start = session_meta.get("money_start")
    money_end = payload.get("total_money") or session_meta.get("money_end")
    money_delta = session_meta.get("money_net_delta")
    runtime_environment = payload.get("runtime_environment")
    if not isinstance(runtime_environment, dict):
        runtime_environment = session_meta.get("runtime_environment")
    if not isinstance(runtime_environment, dict):
        runtime_environment = {}
    outbox = payload.get("outbox")
    if not isinstance(outbox, dict):
        outbox = session_meta.get("outbox")
    if not isinstance(outbox, dict):
        outbox = {}

    lines = [
        f"游戏 Logoff Telemetry Report ({_now_title()})",
        "",
        f"received_at: {received_at}",
        f"user_id: {user_id}",
        f"username: {username or ''}",
        f"session_id: {session_id}",
        f"client_version: {client_version}",
        f"reason: {payload.get('reason') or ''}",
        f"duration: {_format_duration(duration) or duration}",
        (
            "runtime: "
            f"{runtime_environment.get('platform', '')} "
            f"app={runtime_environment.get('application_version', '')} "
            f"unity={runtime_environment.get('unity_version', '')}"
        ),
        f"game_days_total: {days_total}",
        f"game_day_range: {game_day_start} -> {game_day_end}",
        f"telemetry_events: {events_total}",
        (
            "ai_events: "
            f"request={ai_activity['request_events']} "
            f"response={ai_activity['response_events']} "
            f"token_records={token_usage.get('token_usage_events', 0) if token_usage else 0}"
        ),
        f"island_level: {payload.get('island_level') or session_meta.get('island_level_max') or ''}",
        f"money: {money_start} -> {money_end} (delta={money_delta})",
        (
            "money_flow: "
            f"earned={session_meta.get('money_total_earned', '')} "
            f"spent={session_meta.get('money_total_spent', '')} "
            f"reconciliation_ok={session_meta.get('money_reconciliation_ok', '')}"
        ),
        f"tasks: {tasks_completed}/{tasks_total}",
        f"current_task: {payload.get('current_task_title') or task_progress.get('current_task_title') or ''}",
        f"achievements: {payload.get('achievements_unlocked')}/{payload.get('achievements_total')}",
        (
            "outbox: "
            f"pending={outbox.get('pending_count', '')} "
            f"last_success={outbox.get('last_flush_success_count', '')} "
            f"last_failure={outbox.get('last_flush_failure_count', '')}"
        ),
    ]
    lines.extend(_format_ai_token_usage_lines(token_usage))
    lines.extend(_format_user_aggregate_lines(user_aggregate))
    if document_url:
        lines.extend(["", f"full_report_doc: {document_url}"])
    return _clean_text("\n".join(lines), max_chars=2600)


def _build_logoff_report_document_text(
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    received_at: str,
) -> str:
    summary = _build_logoff_report_summary(
        payload=payload,
        headers=headers,
        received_at=received_at,
    )
    telemetry = payload.get("gameplay_telemetry")
    if not isinstance(telemetry, dict):
        telemetry = {}
    session_meta = telemetry.get("session_meta")
    if not isinstance(session_meta, dict):
        session_meta = {}
    runtime_environment = payload.get("runtime_environment")
    if not isinstance(runtime_environment, dict):
        runtime_environment = session_meta.get("runtime_environment")
    if not isinstance(runtime_environment, dict):
        runtime_environment = {}
    outbox = payload.get("outbox")
    if not isinstance(outbox, dict):
        outbox = session_meta.get("outbox")
    if not isinstance(outbox, dict):
        outbox = {}
    ai_activity = _ai_activity_summary(telemetry)
    token_usage = _extract_ai_token_usage(telemetry)
    user_id = headers.get("x-user-id") or payload.get("user_id") or payload.get("player_id") or ""
    session_id = headers.get("x-session-id") or payload.get("session_id") or session_meta.get("session_id") or ""
    duration = payload.get("session_duration_sec") or session_meta.get("game_duration_sec") or ""
    days_total = session_meta.get("game_days_total") or len(telemetry.get("days") or {})
    current_aggregate = _current_session_aggregate(
        session_id=str(session_id or ""),
        duration=duration,
        days_total=days_total,
        game_day_end=session_meta.get("game_day_end"),
        island_level=payload.get("island_level") or session_meta.get("island_level_max"),
        ai_activity=ai_activity,
        token_usage=token_usage,
        session_started_iso=session_meta.get("real_time_started_iso"),
    )
    user_aggregate = _query_user_gameplay_aggregate(
        user_id=str(user_id or ""),
        session_id=str(session_id or ""),
        current=current_aggregate,
    )
    token_lines = _format_ai_token_usage_lines(token_usage)
    token_text = "\n".join(token_lines) if token_lines else "not_found"
    user_aggregate_lines = _format_user_aggregate_lines(user_aggregate)
    user_aggregate_text = "\n".join(user_aggregate_lines) if user_aggregate_lines else "not_found"
    body = {
        "summary": summary,
        "derived_metrics": {
            "ai_activity": ai_activity,
            "ai_token_usage": token_usage,
            "user_aggregate": user_aggregate,
            "runtime_environment": runtime_environment,
            "outbox": outbox,
            "logoff_reason": payload.get("reason"),
            "money_reconciliation_ok": session_meta.get("money_reconciliation_ok"),
            "money_total_earned": session_meta.get("money_total_earned"),
            "money_total_spent": session_meta.get("money_total_spent"),
        },
        "headers": {
            "x-user-id": headers.get("x-user-id"),
            "x-session-id": headers.get("x-session-id"),
            "x-client-version": headers.get("x-client-version"),
            "content-type": headers.get("content-type"),
            "x-encrypted": headers.get("x-encrypted"),
            "content-length": headers.get("content-length"),
        },
        "payload": payload,
    }
    return (
        "Logoff Telemetry Full Report\n\n"
        f"{summary}\n\n"
        "AI Token Usage\n"
        f"{token_text}\n\n"
        "User Aggregate\n"
        f"{user_aggregate_text}\n\n"
        "Derived Metrics\n"
        f"{json.dumps(body['derived_metrics'], ensure_ascii=False, indent=2, default=str)}\n\n"
        "Full JSON\n"
        f"{json.dumps(body, ensure_ascii=False, indent=2, default=str)}"
    )


async def send_logoff_telemetry_report(
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    received_at: str,
) -> None:
    if not _is_logoff_report_enabled():
        return

    if _is_unity_dev_payload(payload, headers):
        return

    missing = _missing_logoff_report_config()
    if missing:
        log(f"[WARNING] Feishu logoff report skipped; missing env: {', '.join(missing)}")
        return

    timeout = float(os.getenv("FEISHU_TIMEOUT", str(_DEFAULT_TIMEOUT)))
    user_id = headers.get("x-user-id") or payload.get("user_id") or payload.get("player_id") or "unknown"
    session_id = headers.get("x-session-id") or payload.get("session_id") or "unknown"
    title = f"Telemetry Logoff Report - {user_id[:8]} - {session_id[:8]} - {_now_title()}"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            token = await _get_tenant_access_token(client)
            if not token:
                return

            document_id = await _create_docx(client, token, title)
            if not document_id:
                return

            document_text = _build_logoff_report_document_text(
                payload=payload,
                headers=headers,
                received_at=received_at,
            )
            if not await _append_docx_text(client, token, document_id, document_text):
                return

            if not await _grant_docx_full_access(client, token, document_id):
                log(f"[WARNING] Feishu logoff report doc created without admin full access: {document_id}")

            summary = _build_logoff_report_summary(
                payload=payload,
                headers=headers,
                received_at=received_at,
                document_url=_doc_url(document_id),
            )
            await _send_text_message(client, token, summary)
    except Exception as exc:
        log(f"[WARNING] Feishu logoff telemetry report failed: {exc}")
