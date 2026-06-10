import asyncio
import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

import aiofiles

from app.core.config import DB_PATH as DEFAULT_DB_PATH
from app.core.config import ERROR_LOG_DIR, GAMEPLAY_TELEMETRY_DIR, OUTPUT_DIR
from app.core.logging import log
from playtime_store import record_play_session_event_to_db

SENSITIVE_HEADERS = {"authorization", "x-api-key", "api-key", "cookie", "set-cookie"}
SENSITIVE_BODY_KEYS = {
    "api_key",
    "apikey",
    "apiKey",
    "provider_api_key",
    "providerApiKey",
    "providerapikey",
    "llm_api_key",
    "llmApiKey",
    "llmapikey",
    "access_key",
    "accessKey",
    "accesskey",
    "secret_key",
    "secretKey",
    "secretkey",
    "token",
}
SESSION_FILE_INDEX: dict[str, Path] = {}
SESSION_FILE_LOCKS: dict[str, asyncio.Lock] = {}


def _header_value(headers: dict, name: str) -> str:
    lower_name = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lower_name and value is not None:
            return str(value)
    return ""


def _safe_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in SENSITIVE_HEADERS}


def _safe_body(value):
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            if str(key) in SENSITIVE_BODY_KEYS or str(key).lower() in SENSITIVE_BODY_KEYS:
                safe[key] = "[REDACTED]"
            else:
                safe[key] = _safe_body(item)
        return safe
    if isinstance(value, list):
        return [_safe_body(item) for item in value]
    return value


def _infer_event_source(payload: object) -> str:
    if not isinstance(payload, dict):
        return "unknown"
    launcher_keys = {
        "launch_duration_ms",
        "launch_args",
        "windows_version",
        "windows_build",
        "windows_arch",
        "windows_locale",
        "gpu_name",
        "gpu_device_id",
        "gpu_driver_version",
        "process_visible_duration_sec",
        "exit_code",
    }
    if launcher_keys.intersection(payload.keys()):
        return "launcher"
    return "python"


def _merge_dict(existing: dict, incoming: dict) -> dict:
    merged = dict(existing)
    for key, value in incoming.items():
        if key not in merged or merged[key] in (None, "", []):
            merged[key] = value
    return merged


def _first_nonempty(*values) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _safe_filename_part(value: str, fallback: str) -> str:
    text = value.strip() if value else fallback
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in text)
    safe = safe.strip("._")
    return safe[:80] if safe else fallback


def _get_session_id(headers: dict, payload: object) -> str:
    session_id = _header_value(headers, "x-session-id")
    if not session_id and isinstance(payload, dict):
        session_id = payload.get("session_id")
    return session_id or f"unknown-{uuid.uuid4().hex[:8]}"


def _get_session_meta(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    telemetry = payload.get("gameplay_telemetry")
    if not isinstance(telemetry, dict):
        return {}
    session_meta = telemetry.get("session_meta")
    return session_meta if isinstance(session_meta, dict) else {}


def _build_gameplay_telemetry_ingest_record(
    *,
    payload: dict,
    headers: dict,
    event_type: str,
    timestamp_iso: str,
    decrypted_body_bytes: int,
) -> dict:
    safe_payload = _safe_body(payload)
    session_meta = _get_session_meta(payload)
    safe_headers = _safe_headers(headers)
    session_id = _first_nonempty(
        _header_value(headers, "x-session-id"),
        payload.get("session_id"),
        session_meta.get("session_id"),
    )
    user_id = _first_nonempty(
        _header_value(headers, "x-user-id"),
        payload.get("user_id"),
        payload.get("player_id"),
        session_meta.get("user_id"),
    )
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
        session_meta.get("player_session_id"),
        session_meta.get("playerSessionId"),
    )
    client_version = _first_nonempty(
        _header_value(headers, "x-client-version"),
        payload.get("client_version"),
        session_meta.get("client_version"),
    )
    record = {
        **safe_payload,
        "ingest": {
            "type": "gameplay_telemetry_ingest",
            "id": uuid.uuid4().hex,
            "endpoint": "/logoff",
            "event_type": event_type,
            "received_at": timestamp_iso,
            "user_id": user_id,
            "session_id": session_id,
            "player_session_id": player_session_id,
            "player_id": player_id,
            "client_version": client_version,
            "outbox_id": _header_value(headers, "x-outbox-id"),
            "headers": safe_headers,
            "payload_size_bytes": decrypted_body_bytes,
            "import_status": "pending",
            "import_error": "",
        },
        "user_id": user_id or safe_payload.get("user_id") or safe_payload.get("player_id"),
        "player_id": player_id or safe_payload.get("player_id"),
        "session_id": session_id or safe_payload.get("session_id"),
        "player_session_id": player_session_id
        or safe_payload.get("player_session_id")
        or safe_payload.get("playerSessionId"),
        "client_version": client_version or safe_payload.get("client_version"),
        "country": _first_nonempty(
            _header_value(headers, "cf-ipcountry"),
            safe_payload.get("country"),
            safe_payload.get("country_code"),
        ),
    }
    return record


def _session_file_path(user_id: str | None, session_id: str) -> Path:
    user_folder = user_id if user_id else "anonymous"
    user_dir = OUTPUT_DIR / user_folder
    return user_dir / f"session-{session_id}.jsonl"


def _find_existing_session_file(session_id: str) -> Path | None:
    cached_path = SESSION_FILE_INDEX.get(session_id)
    if cached_path and cached_path.exists():
        return cached_path
    pattern = f"session-{session_id}.jsonl"
    for path in OUTPUT_DIR.rglob(pattern):
        SESSION_FILE_INDEX[session_id] = path
        return path
    return None


async def _load_session_record(filepath: Path) -> dict | None:
    try:
        async with aiofiles.open(filepath, "r", encoding="utf-8") as f:
            lines = [line.strip() async for line in f if line.strip()]
        if not lines:
            return None
        return json.loads(lines[-1])
    except Exception:
        return None


def _write_session_record_atomic(filepath: Path, record: dict) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{filepath.name}.",
        suffix=".tmp",
        dir=filepath.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
        if hasattr(os, "O_DIRECTORY"):
            dir_fd = os.open(filepath.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


async def _save_session_record(filepath: Path, record: dict) -> None:
    await asyncio.to_thread(_write_session_record_atomic, filepath, record)


async def save_gameplay_telemetry_ingest(
    *,
    payload: dict,
    headers: dict,
    event_type: str,
    timestamp_iso: str,
    decrypted_body_bytes: int,
) -> Path:
    record = _build_gameplay_telemetry_ingest_record(
        payload=payload,
        headers=headers,
        event_type=event_type,
        timestamp_iso=timestamp_iso,
        decrypted_body_bytes=decrypted_body_bytes,
    )
    ingest = record["ingest"]
    user_part = _safe_filename_part(ingest.get("user_id") or "", "anonymous")
    session_part = _safe_filename_part(
        ingest.get("player_session_id") or ingest.get("session_id") or "",
        "unknown-session",
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    filename = f"{timestamp}-{ingest['id'][:8]}.json"
    filepath = GAMEPLAY_TELEMETRY_DIR / user_part / session_part / filename
    await asyncio.to_thread(_write_session_record_atomic, filepath, record)
    return filepath


def _configured_db_path() -> str | None:
    db_path = os.getenv("DB_PATH")
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    db_path = str(db_path).strip()
    return db_path or None


async def save_play_session_event(
    *,
    payload: dict,
    headers: dict,
    event_type: str,
    timestamp_iso: str,
    decrypted_body_bytes: int,
) -> dict | None:
    db_path = _configured_db_path()
    if not db_path:
        log("[WARNING] DB_PATH is empty; skipped play session event persistence")
        return None

    safe_payload = _safe_body(payload)
    safe_headers = _safe_headers(headers)
    return await asyncio.to_thread(
        record_play_session_event_to_db,
        db_path=db_path,
        payload=safe_payload,
        headers=safe_headers,
        event_type=event_type,
        received_at=timestamp_iso,
        payload_size_bytes=decrypted_body_bytes,
    )


async def save_request_to_file(
    request_body: bytes,
    path: str,
    method: str,
    headers: dict,
    user_id: str | None,
    filepath: Path,
    timestamp_iso: str,
) -> None:
    try:
        try:
            request_json = json.loads(request_body) if request_body else None
        except json.JSONDecodeError:
            request_json = request_body.decode("utf-8", errors="replace")
        request_json = _safe_body(request_json)

        safe_headers = _safe_headers(headers)

        request_record = {
            "type": "request",
            "timestamp": timestamp_iso,
            "user_id": user_id,
            "method": method,
            "path": path,
            "headers": safe_headers,
            "body": request_json,
        }

        async with aiofiles.open(filepath, "a", encoding="utf-8") as f:
            await f.write(json.dumps(request_record, ensure_ascii=False) + "\n")
    except Exception as exc:
        log(f"[WARNING] Failed to save request log: {exc}")


async def save_response_to_file(
    response_json: dict | None,
    response_status: int,
    duration_ms: float,
    user_id: str | None,
    filepath: Path,
    timestamp_iso: str,
) -> None:
    try:
        usage = None
        if response_json and "usage" in response_json:
            usage = response_json["usage"]

        response_record = {
            "type": "response",
            "timestamp": timestamp_iso,
            "user_id": user_id,
            "duration_ms": round(duration_ms, 2),
            "status_code": response_status,
            "usage": usage,
            "body": response_json,
        }

        async with aiofiles.open(filepath, "a", encoding="utf-8") as f:
            await f.write(json.dumps(response_record, ensure_ascii=False) + "\n")
    except Exception as exc:
        log(f"[WARNING] Failed to save response log: {exc}")


async def update_session_event_log(
    *,
    raw_body: bytes,
    headers: dict,
    event_type: str,
    timestamp_iso: str,
    raise_on_error: bool = False,
) -> Path | None:
    try:
        try:
            payload = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            payload = raw_body.decode("utf-8", errors="replace")

        safe_headers = _safe_headers(headers)
        session_id = _get_session_id(headers, payload)
        source = _infer_event_source(payload)
        incoming_user_id = headers.get("x-user-id")

        lock = SESSION_FILE_LOCKS.setdefault(session_id, asyncio.Lock())
        async with lock:
            existing_path = _find_existing_session_file(session_id)
            record = await _load_session_record(existing_path) if existing_path else None

            if not record:
                record = {
                    "type": "session",
                    "session_id": session_id,
                    "user_id": None,
                    "created_at": timestamp_iso,
                    "updated_at": timestamp_iso,
                    "sources": {},
                }

            if incoming_user_id and (record.get("user_id") is None or source == "python"):
                record["user_id"] = incoming_user_id

            record["updated_at"] = timestamp_iso
            sources = record.setdefault("sources", {})
            source_bucket = sources.setdefault(source, {})
            event_bucket = source_bucket.setdefault(event_type, {})
            event_bucket["received_at"] = timestamp_iso
            if isinstance(payload, dict) and payload.get("timestamp"):
                event_bucket["event_timestamp"] = payload.get("timestamp")
            if "headers" in event_bucket:
                event_bucket["headers"] = _merge_dict(event_bucket["headers"], safe_headers)
            else:
                event_bucket["headers"] = safe_headers
            if "payload" in event_bucket and isinstance(payload, dict):
                event_bucket["payload"] = _merge_dict(event_bucket["payload"], payload)
            else:
                event_bucket["payload"] = payload

            target_path = _session_file_path(record.get("user_id"), session_id)
            if existing_path and existing_path != target_path:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                existing_path.replace(target_path)

            await _save_session_record(target_path, record)
            SESSION_FILE_INDEX[session_id] = target_path
            return target_path
    except Exception as exc:
        log(f"[WARNING] Failed to update session log: {exc}")
        if raise_on_error:
            raise
        return None


async def save_error_log_to_file(
    request_body: bytes,
    headers: dict,
    user_id: str | None,
    filepath: Path,
    timestamp_iso: str,
) -> None:
    try:
        try:
            request_json = json.loads(request_body) if request_body else None
        except json.JSONDecodeError:
            request_json = request_body.decode("utf-8", errors="replace")
        request_json = _safe_body(request_json)

        safe_headers = {
            k: v for k, v in headers.items() if k.lower() not in SENSITIVE_HEADERS
        }

        error_record = {
            "type": "error_log",
            "timestamp": timestamp_iso,
            "user_id": user_id,
            "headers": safe_headers,
            "body": request_json,
        }

        async with aiofiles.open(filepath, "a", encoding="utf-8") as f:
            await f.write(json.dumps(error_record, ensure_ascii=False) + "\n")
    except Exception as exc:
        log(f"[WARNING] Failed to save error log: {exc}")


def build_log_filepath(user_id: str | None, subdir: str | None = None) -> tuple[Path, str]:
    user_folder = user_id if user_id else "anonymous"
    user_dir = OUTPUT_DIR / user_folder
    if subdir:
        user_dir = user_dir / subdir
    user_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    timestamp_iso = datetime.now().isoformat()
    unique_id = str(uuid.uuid4())[:8]
    filename = f"{timestamp}-{unique_id}.jsonl"
    filepath = user_dir / filename
    return filepath, timestamp_iso


def ensure_error_log_dir() -> None:
    ERROR_LOG_DIR.mkdir(parents=True, exist_ok=True)
