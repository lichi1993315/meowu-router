import datetime
import json
import os
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.core.logging import log


_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
_DEFAULT_TIMEOUT = 20.0
_DEFAULT_MAX_CHARS = 3500


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _is_enabled() -> bool:
    value = os.getenv("FEISHU_ERROR_LOG_ALERTS", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _missing_config() -> list[str]:
    required = ("FEISHU_BOT_API_KEY", "FEISHU_BOT_API_SECRET", "FEISHU_CHAT_ID")
    return [name for name in required if not _env(name)]


def _clean_text(value: Any, *, max_chars: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 20].rstrip() + "\n...[truncated]"


def _now_title() -> str:
    try:
        tzinfo = ZoneInfo(os.getenv("REPORT_TIMEZONE", "Asia/Shanghai"))
    except Exception:
        tzinfo = ZoneInfo("Asia/Shanghai")
    return datetime.datetime.now(tzinfo).strftime("%Y-%m-%d %H:%M:%S")


def _build_error_log_alert_text(
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    received_at: str,
    decrypted_body_bytes: int,
) -> str:
    user_id = headers.get("x-user-id") or payload.get("user_id") or payload.get("player_id") or ""
    session_id = headers.get("x-session-id") or payload.get("session_id") or ""
    client_version = headers.get("x-client-version") or payload.get("client_version") or ""
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


async def send_error_log_alert(
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    received_at: str,
    decrypted_body_bytes: int,
) -> None:
    if not _is_enabled():
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
    receive_id_type = os.getenv("FEISHU_RECEIVE_ID_TYPE", "chat_id").strip() or "chat_id"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            token = await _get_tenant_access_token(client)
            if not token:
                return

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
                log(f"[WARNING] Feishu error_log alert send failed: {data}")
    except Exception as exc:
        log(f"[WARNING] Feishu error_log alert failed: {exc}")
