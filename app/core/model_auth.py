"""Router credentials are independent of upstream provider credentials."""
import json
import os
import secrets

from fastapi import HTTPException, Request

from app.utils.crypto import FernetConfigError, decrypt_payload


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="Router authentication required")


async def require_model_access(request: Request) -> None:
    keys = [key.strip().encode() for key in os.getenv("ROUTER_API_KEYS", "").split(",") if key.strip()]
    supplied = request.headers.get("x-router-key")
    if supplied is not None:
        # Check every configured key; do not reinterpret upstream Authorization.
        matched = False
        for key in keys:
            matched |= secrets.compare_digest(supplied.encode(), key)
        if not matched:
            raise _unauthorized()
        request.state.router_auth_method = "router_key"
        return

    legacy = os.getenv("ROUTER_ALLOW_LEGACY_ENCRYPTED", "false").lower() == "true"
    if legacy and request.headers.get("x-encrypted", "").lower() == "true":
        try:
            plaintext = decrypt_payload(await request.body())
            if plaintext is None or not isinstance(json.loads(plaintext), dict):
                raise _unauthorized()
        except FernetConfigError as exc:
            raise HTTPException(status_code=503, detail="Router authentication unavailable") from exc
        except (UnicodeError, ValueError, TypeError) as exc:
            raise _unauthorized() from exc
        # Reuse the verified payload; never trust a client-provided marker alone.
        request.state.verified_model_body = plaintext.encode("utf-8")
        request.state.router_auth_method = "legacy_encrypted"
        return

    raise _unauthorized()
