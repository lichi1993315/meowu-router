import json

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.responses import JSONResponse

from app.main import create_app
from app.api.routes import gemini, embeddings
from app.services import gemini_proxy, sessions

PATHS = [prefix + suffix for prefix in ("", "/api")
         for suffix in ("/chat/completions", "/v1/chat/completions", "/embeddings", "/v1/embeddings")]


@pytest.fixture
def client_setup(monkeypatch):
    monkeypatch.setenv("ROUTER_API_KEYS", "first-key,second-key")
    monkeypatch.delenv("ROUTER_ALLOW_LEGACY_ENCRYPTED", raising=False)
    key = Fernet.generate_key()
    fernet = Fernet(key)
    monkeypatch.setenv("PAW_FERNET_KEY", key.decode())
    calls = []

    async def handler(**kwargs):
        request = kwargs["request"]
        raw = await request.body()
        decoded = gemini_proxy._maybe_decrypt(request, raw)
        calls.append((json.loads(decoded), request.headers.get("authorization"),
                      request.state.router_auth_method))
        return JSONResponse({"ok": True})

    monkeypatch.setattr(gemini, "handle_chat_completions", handler)
    monkeypatch.setattr(embeddings, "handle_embeddings", handler)
    app = create_app()
    app.state.http_client = None
    app.state.blacklist = set()
    return app, calls, fernet


@pytest.mark.parametrize("path", PATHS)
async def test_anonymous_and_invalid_credentials_never_reach_model(client_setup, path):
    app, calls, _ = client_setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client:
        for headers in ({}, {"X-Router-Key": "wrong"}, {"Authorization": "Bearer provider-key"},
                        {"X-API-Key": "provider-key"}, {"X-Encrypted": "true"}):
            response = await client.post(path, json={}, headers=headers)
            assert response.status_code == 401
    assert calls == []


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("key", ["first-key", "second-key"])
async def test_valid_keys_preserve_upstream_authorization(client_setup, path, key):
    app, calls, _ = client_setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client:
        response = await client.post(path, json={}, headers={"X-Router-Key": key, "Authorization": "Bearer provider-key"})
        assert response.status_code == 200
    assert calls == [({}, "Bearer provider-key", "router_key")]


@pytest.mark.parametrize("path", PATHS)
async def test_legacy_requires_verified_ciphertext_and_explicit_switch(client_setup, monkeypatch, path):
    app, calls, fernet = client_setup
    token = fernet.encrypt(b'{"messages": []}')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client:
        assert (await client.post(path, content=token, headers={"X-Encrypted": "true"})).status_code == 401
        monkeypatch.setenv("ROUTER_ALLOW_LEGACY_ENCRYPTED", "true")
        for body in (b"{}", b"not-a-token", fernet.encrypt(b"[]"), fernet.encrypt(b"not-json"), Fernet(Fernet.generate_key()).encrypt(b"{}")):
            assert (await client.post(path, content=body, headers={"X-Encrypted": "true"})).status_code == 401
        assert (await client.post(path, content=token, headers={"X-Encrypted": "true", "X-Router-Key": "wrong"})).status_code == 401
        assert calls == []
        assert (await client.post(path, content=token, headers={"X-Encrypted": "true"})).status_code == 200
    assert calls == [({"messages": []}, None, "legacy_encrypted")]


async def test_missing_configuration_fails_closed(client_setup, monkeypatch):
    app, calls, fernet = client_setup
    monkeypatch.setenv("ROUTER_API_KEYS", " , ")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client:
        assert (await client.post(PATHS[0], json={})).status_code == 401
        assert (await client.post(PATHS[0], json={}, headers={"X-Router-Key": "first-key"})).status_code == 401
        monkeypatch.setenv("ROUTER_ALLOW_LEGACY_ENCRYPTED", "true")
        monkeypatch.delenv("PAW_FERNET_KEY")
        assert (await client.post(PATHS[0], content=fernet.encrypt(b"{}"), headers={"X-Encrypted": "true"})).status_code == 503
    assert calls == []


async def test_health_remains_public(client_setup):
    app, _, _ = client_setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client:
        for path in ("/health", "/api/health"):
            assert (await client.get(path)).status_code == 200


def test_router_key_is_redacted_case_insensitively():
    assert sessions._safe_headers({"X-Router-Key": "secret", "x-router-key": "secret", "Authorization": "provider", "X-User-ID": "player"}) == {"X-User-ID": "player"}
