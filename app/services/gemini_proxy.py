import asyncio
import json
from datetime import datetime

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.config import GEMINI_API_KEY, GEMINI_BASE_URL, GEMINI_SDK_URL
from app.core.logging import log
from app.services import sessions
from app.utils.crypto import decrypt_payload


def _get_user_id(request: Request) -> str:
    return request.headers.get("x-user-id") or "anonymous_user"


def _check_blacklist(user_id: str, blacklist: set[str]) -> None:
    if user_id and user_id in blacklist:
        log(f"🚫 Blocked blacklisted user: {user_id}")
        raise HTTPException(status_code=403, detail="Access denied")


def _maybe_decrypt(request: Request, raw_body: bytes) -> bytes:
    is_encrypted = request.headers.get("x-encrypted", "").lower() == "true"
    if not is_encrypted:
        return raw_body

    encrypted_str = raw_body.decode("utf-8")
    decrypted_str = decrypt_payload(encrypted_str)
    if decrypted_str is None:
        raise HTTPException(status_code=400, detail="Failed to decrypt request body")
    return decrypted_str.encode("utf-8")


async def handle_chat_completions(
    request: Request,
    http_client: httpx.AsyncClient,
    blacklist: set[str],
) -> JSONResponse:
    api_key = GEMINI_API_KEY
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not set")

    try:
        raw_body = await request.body()
        user_id = _get_user_id(request)
        _check_blacklist(user_id, blacklist)

        body = _maybe_decrypt(request, raw_body)

        filepath, timestamp_iso = sessions.build_log_filepath(user_id)

        asyncio.create_task(
            sessions.save_request_to_file(
                request_body=body,
                path=str(request.url.path),
                method=request.method,
                headers=dict(request.headers),
                user_id=user_id,
                filepath=filepath,
                timestamp_iso=timestamp_iso,
            )
        )

        start_time = datetime.now()

        response = await http_client.post(
            f"{GEMINI_BASE_URL}/chat/completions",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )

        duration_ms = (datetime.now() - start_time).total_seconds() * 1000
        response_json = response.json()

        asyncio.create_task(
            sessions.save_response_to_file(
                response_json=response_json,
                response_status=response.status_code,
                duration_ms=duration_ms,
                user_id=user_id,
                filepath=filepath,
                timestamp_iso=timestamp_iso,
            )
        )

        return JSONResponse(content=response_json, status_code=response.status_code)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream timeout")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


async def handle_embeddings(
    request: Request,
    http_client: httpx.AsyncClient,
    blacklist: set[str],
) -> JSONResponse:
    api_key = GEMINI_API_KEY
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not set")

    try:
        raw_body = await request.body()
        user_id = _get_user_id(request)
        _check_blacklist(user_id, blacklist)

        body = _maybe_decrypt(request, raw_body)

        filepath, timestamp_iso = sessions.build_log_filepath(user_id, subdir="embedding")

        asyncio.create_task(
            sessions.save_request_to_file(
                request_body=body,
                path=str(request.url.path),
                method=request.method,
                headers=dict(request.headers),
                user_id=user_id,
                filepath=filepath,
                timestamp_iso=timestamp_iso,
            )
        )

        start_time = datetime.now()

        try:
            request_json = json.loads(body) if body else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        model = request_json.get("model") or "gemini-embedding-001"
        content_value = request_json.get("content")
        requests_value = request_json.get("requests")
        task_type = request_json.get("taskType") or request_json.get("task_type")

        base_url = GEMINI_SDK_URL.rstrip("/")
        use_batch = False

        if requests_value is not None or content_value is not None:
            model_id = model
            if not model_id:
                raise HTTPException(status_code=400, detail="Missing 'model' field")

            if model_id.startswith("models/"):
                model_id = model_id[len("models/") :]

            if requests_value is not None:
                if not isinstance(requests_value, list):
                    raise HTTPException(status_code=400, detail="'requests' must be a list")
                if not requests_value:
                    raise HTTPException(status_code=400, detail="Empty 'requests' field")

                use_batch = len(requests_value) > 1
                requests_payload = []
                for request_entry in requests_value:
                    if not isinstance(request_entry, dict):
                        raise HTTPException(status_code=400, detail="Invalid 'requests' entry")

                    content = request_entry.get("content")
                    if content is None:
                        raise HTTPException(
                            status_code=400, detail="Missing 'content' in requests entry"
                        )

                    request_payload = {"content": content}
                    entry_task_type = request_entry.get("taskType") or task_type
                    if entry_task_type:
                        request_payload["taskType"] = entry_task_type
                    if use_batch:
                        request_payload["model"] = (
                            request_entry.get("model") or f"models/{model_id}"
                        )
                    requests_payload.append(request_payload)

                if use_batch:
                    payload = {"requests": requests_payload}
                    endpoint = f"{base_url}/models/{model_id}:batchEmbedContents"
                else:
                    payload = requests_payload[0]
                    endpoint = f"{base_url}/models/{model_id}:embedContent"
            else:
                content = content_value
                if not isinstance(content, dict):
                    raise HTTPException(status_code=400, detail="Invalid 'content' field")

                payload = {"content": content}
                if task_type:
                    payload["taskType"] = task_type
                endpoint = f"{base_url}/models/{model_id}:embedContent"
        else:
            raise HTTPException(status_code=400, detail="Missing 'content' or 'requests' field")

        response = await http_client.post(
            endpoint,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
        )

        duration_ms = (datetime.now() - start_time).total_seconds() * 1000
        response_json = response.json()

        if response.is_success:
            if use_batch:
                embeddings = response_json.get("embeddings", [])
                data = []
                for index, embedding in enumerate(embeddings):
                    values = embedding.get("values") if isinstance(embedding, dict) else embedding
                    data.append({
                        "object": "embedding",
                        "index": index,
                        "embedding": values,
                    })
            else:
                embedding_obj = response_json.get("embedding", {})
                values = (
                    embedding_obj.get("values")
                    if isinstance(embedding_obj, dict)
                    else embedding_obj
                )
                data = [
                    {
                        "object": "embedding",
                        "index": 0,
                        "embedding": values,
                    }
                ]

            response_json = {
                "object": "list",
                "data": data,
                "model": model,
            }

        asyncio.create_task(
            sessions.save_response_to_file(
                response_json=response_json,
                response_status=response.status_code,
                duration_ms=duration_ms,
                user_id=user_id,
                filepath=filepath,
                timestamp_iso=timestamp_iso,
            )
        )

        return JSONResponse(content=response_json, status_code=response.status_code)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream timeout")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
