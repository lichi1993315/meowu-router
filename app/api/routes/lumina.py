"""Lumina PawFishing (web) playtest events. Contract: lumina_pawfishing docs/ANALYTICS.md.

A browser cannot keep a Fernet key, so this route takes plain JSON (sent as text/plain: a simple
CORS request, allowed while a page closes) with a per-playtest token from LUMINA_INGEST_TOKENS
(comma-separated; remove one and restart router-api to revoke it), and a per-IP request limit.
"""
import asyncio
import ipaddress
import json
import os
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from sqlite_runtime import is_busy, MigrationRequired

router = APIRouter()
MAX_BODY = 262144
RATE_PER_MINUTE = int(os.getenv('LUMINA_RATE_PER_MINUTE', '120'))
_hits: dict[str, deque] = defaultdict(deque)


def _tokens():
    return {t.strip() for t in os.getenv('LUMINA_INGEST_TOKENS', '').split(',') if t.strip()}


def _client_ip(request: Request) -> str:
    # Forwarding headers are trusted only from the ingress itself (same rule as /feedback).
    ip = request.client.host if request.client else 'unknown'
    trusted = os.getenv('LUMINA_TRUSTED_PROXY_IPS') or os.getenv('FEEDBACK_TRUSTED_PROXY_IPS', '')
    if ip in {v.strip() for v in trusted.split(',') if v.strip()}:
        candidate = request.headers.get('x-forwarded-for', '').split(',')[-1].strip()
        try:
            ip = str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return ip


def _limited(ip: str) -> bool:
    now = time.monotonic()
    hits = _hits[ip]
    while hits and now - hits[0] > 60:
        hits.popleft()
    if len(hits) >= RATE_PER_MINUTE:
        return True
    hits.append(now)
    if len(_hits) > 10000:
        for key in [k for k, v in _hits.items() if not v]:
            del _hits[key]
    return False


@router.post('/v1/lumina/events')
async def lumina_events(request: Request):
    from app.core.config import DB_PATH
    from lumina_analytics import accept_lumina_batch
    tokens = _tokens()
    if not tokens:
        raise HTTPException(503, 'lumina ingest is not configured')
    if _limited(_client_ip(request)):
        return JSONResponse(status_code=429, headers={'Retry-After': '60'}, content={'detail': 'rate limited'})
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            raise HTTPException(413, 'batch too large')
        body.extend(chunk)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, 'body is not json') from exc
    if not isinstance(payload, dict) or payload.get('token') not in tokens:
        raise HTTPException(401, 'invalid ingest token')
    try:
        return await asyncio.to_thread(accept_lumina_batch, DB_PATH, payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except MigrationRequired as exc:
        raise HTTPException(503, 'lumina schema not migrated') from exc
    except Exception as exc:
        if is_busy(exc):
            return JSONResponse(status_code=503, headers={'Retry-After': '15'}, content={'detail': 'telemetry store busy', 'retryable': True})
        raise
