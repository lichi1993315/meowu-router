import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

from app.core.config import ERROR_LOG_DIR
from app.core.logging import log
from app.services import feishu_alerts, sessions
from app.services.leaderboard import get_leaderboard_data
from app.utils.crypto import FernetConfigError, decrypt_payload

router = APIRouter()


def _log_game_telemetry_request(
    request: Request,
    *,
    encrypted_body_bytes: int,
    decrypted_body_bytes: int | None = None,
    validation_error: str | None = None,
) -> None:
    record: dict[str, Any] = {
        "endpoint": str(request.url.path),
        "user_id": request.headers.get("x-user-id", ""),
        "session_id": request.headers.get("x-session-id", ""),
        "client_version": request.headers.get("x-client-version", ""),
        "encrypted_body_bytes": encrypted_body_bytes,
    }
    if decrypted_body_bytes is not None:
        record["decrypted_body_bytes"] = decrypted_body_bytes
    if validation_error:
        record["validation_error"] = validation_error
    log(f"[game_telemetry] {json.dumps(record, ensure_ascii=False)}")


async def read_encrypted_game_telemetry_payload(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    encrypted_body_bytes = len(raw_body)
    encrypted = request.headers.get("x-encrypted", "").strip().lower() == "true"
    if not encrypted:
        detail = "encrypted telemetry required"
        _log_game_telemetry_request(
            request,
            encrypted_body_bytes=encrypted_body_bytes,
            validation_error=detail,
        )
        raise HTTPException(status_code=415, detail=detail)

    token = raw_body.strip()
    if not token:
        detail = "empty telemetry body"
        _log_game_telemetry_request(
            request,
            encrypted_body_bytes=encrypted_body_bytes,
            validation_error=detail,
        )
        raise HTTPException(status_code=400, detail=detail)

    try:
        decrypted = decrypt_payload(token)
    except FernetConfigError as exc:
        detail = "telemetry encryption key is not configured"
        _log_game_telemetry_request(
            request,
            encrypted_body_bytes=encrypted_body_bytes,
            validation_error=detail,
        )
        raise HTTPException(status_code=500, detail=detail) from exc
    except UnicodeDecodeError as exc:
        detail = "decrypted telemetry body is not utf-8"
        _log_game_telemetry_request(
            request,
            encrypted_body_bytes=encrypted_body_bytes,
            validation_error=detail,
        )
        raise HTTPException(status_code=400, detail=detail) from exc

    if decrypted is None:
        detail = "invalid encrypted telemetry body"
        _log_game_telemetry_request(
            request,
            encrypted_body_bytes=encrypted_body_bytes,
            validation_error=detail,
        )
        raise HTTPException(status_code=400, detail=detail)

    decrypted_body_bytes = len(decrypted.encode("utf-8"))
    try:
        payload = json.loads(decrypted)
    except json.JSONDecodeError as exc:
        detail = "decrypted telemetry body is not json"
        _log_game_telemetry_request(
            request,
            encrypted_body_bytes=encrypted_body_bytes,
            decrypted_body_bytes=decrypted_body_bytes,
            validation_error=detail,
        )
        raise HTTPException(status_code=400, detail=detail) from exc

    if not isinstance(payload, dict):
        detail = "telemetry payload must be a json object"
        _log_game_telemetry_request(
            request,
            encrypted_body_bytes=encrypted_body_bytes,
            decrypted_body_bytes=decrypted_body_bytes,
            validation_error=detail,
        )
        raise HTTPException(status_code=400, detail=detail)

    _log_game_telemetry_request(
        request,
        encrypted_body_bytes=encrypted_body_bytes,
        decrypted_body_bytes=decrypted_body_bytes,
    )
    return payload


def _payload_to_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@router.post("/login")
@router.post("/v1/login")
async def login(request: Request):
    try:
        payload = await read_encrypted_game_telemetry_payload(request)
        raw_body = _payload_to_body(payload)
        timestamp_iso = datetime.now().isoformat()
        asyncio.create_task(
            sessions.update_session_event_log(
                raw_body=raw_body,
                headers=dict(request.headers),
                event_type="login",
                timestamp_iso=timestamp_iso,
            )
        )

        return JSONResponse(content={"status": "ok"}, status_code=200)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/logoff")
@router.post("/v1/logoff")
async def logoff(request: Request):
    try:
        payload = await read_encrypted_game_telemetry_payload(request)
        raw_body = _payload_to_body(payload)
        timestamp_iso = datetime.now().isoformat()
        asyncio.create_task(
            sessions.update_session_event_log(
                raw_body=raw_body,
                headers=dict(request.headers),
                event_type="logoff",
                timestamp_iso=timestamp_iso,
            )
        )

        return JSONResponse(content={"status": "ok"}, status_code=200)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/error_log")
@router.post("/v1/error_log")
async def upload_error_log(request: Request):
    try:
        payload = await read_encrypted_game_telemetry_payload(request)
        raw_body = _payload_to_body(payload)
        user_id = request.headers.get("x-user-id")
        headers = dict(request.headers)

        sessions.ensure_error_log_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        timestamp_iso = datetime.now().isoformat()
        unique_id = str(uuid.uuid4())[:8]
        filename = f"{timestamp}-{unique_id}.jsonl"
        filepath = ERROR_LOG_DIR / filename

        asyncio.create_task(
            sessions.save_error_log_to_file(
                request_body=raw_body,
                headers=headers,
                user_id=user_id,
                filepath=filepath,
                timestamp_iso=timestamp_iso,
            )
        )
        asyncio.create_task(
            feishu_alerts.send_error_log_alert(
                payload=payload,
                headers=headers,
                received_at=timestamp_iso,
                decrypted_body_bytes=len(raw_body),
            )
        )

        return JSONResponse(content={"status": "ok"}, status_code=200)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/leaderboard")
@router.get("/v1/leaderboard")
async def leaderboard(request: Request):
    user_id = request.headers.get("x-user-id")
    data = await asyncio.to_thread(get_leaderboard_data, user_id)
    return JSONResponse(content=data)
