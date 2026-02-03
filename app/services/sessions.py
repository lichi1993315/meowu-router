import json
import uuid
from datetime import datetime
from pathlib import Path

import aiofiles

from app.core.config import ERROR_LOG_DIR, OUTPUT_DIR
from app.core.logging import log

SENSITIVE_HEADERS = {"authorization", "x-api-key", "api-key", "cookie", "set-cookie"}
SESSION_FILE_INDEX: dict[str, Path] = {}


def _safe_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in SENSITIVE_HEADERS}


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


def _get_session_id(headers: dict, payload: object) -> str:
    session_id = headers.get("x-session-id")
    if not session_id and isinstance(payload, dict):
        session_id = payload.get("session_id")
    return session_id or f"unknown-{uuid.uuid4().hex[:8]}"


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


async def _save_session_record(filepath: Path, record: dict) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
        await f.write(json.dumps(record, ensure_ascii=False) + "\n")


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
) -> None:
    try:
        try:
            payload = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            payload = raw_body.decode("utf-8", errors="replace")

        safe_headers = _safe_headers(headers)
        session_id = _get_session_id(headers, payload)
        source = _infer_event_source(payload)
        incoming_user_id = headers.get("x-user-id")

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
    except Exception as exc:
        log(f"[WARNING] Failed to update session log: {exc}")


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
