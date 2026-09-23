"""Bounded multipart endpoint with the existing encrypted client metadata contract."""
import asyncio
import json
import os
import ipaddress

from fastapi import APIRouter, HTTPException, Request
from starlette.datastructures import UploadFile

from app.services import player_feedback
from app.utils.crypto import decrypt_payload, FernetConfigError

router = APIRouter()
MAX_BODY = player_feedback.MAX_IMAGE + 128 * 1024


@router.post("/feedback", status_code=202)
async def submit_feedback(request: Request):
    owner = request.headers.get("x-user-id", "").strip()
    if not owner or len(owner) > 128:
        raise HTTPException(400, "Missing or invalid X-User-ID")
    if owner in getattr(request.app.state, "blacklist", set()):
        raise HTTPException(403, "Access denied")
    # Read with a hard bound before invoking the multipart parser (including chunked bodies).
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            raise HTTPException(413, "Feedback too large")
        body.extend(chunk)
    request._body = bytes(body)
    try:
        async with request.form(max_files=1, max_fields=1) as form:
            metadata = form.get("metadata")
            if not isinstance(metadata, str) or len(metadata) > 96 * 1024:
                raise HTTPException(400, "Invalid metadata")
            plain = decrypt_payload(metadata)
            if plain is None:
                raise HTTPException(400, "Invalid encrypted metadata")
            payload = json.loads(plain)
            screenshot = form.get("screenshot")
            if screenshot is not None and not isinstance(screenshot, UploadFile):
                raise HTTPException(400, "Invalid screenshot")
            image = await screenshot.read(player_feedback.MAX_IMAGE + 1) if screenshot else b""
            # Do not trust user-supplied forwarding headers. The ingress must set the ASGI client IP.
            ip = request.client.host if request.client else "unknown"
            trusted = os.getenv("FEEDBACK_TRUSTED_PROXY_IPS", "").split(",")
            if ip in {value.strip() for value in trusted if value.strip()}:
                candidate = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
                try:
                    ip = str(ipaddress.ip_address(candidate))
                except ValueError:
                    pass
            return await asyncio.to_thread(player_feedback.accept, payload, image, owner, ip)
    except player_feedback.FeedbackRejected as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid metadata")
    except FernetConfigError:
        raise HTTPException(503, "Feedback temporarily unavailable")
