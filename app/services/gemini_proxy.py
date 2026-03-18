import asyncio
import json
from datetime import datetime
def _is_image_model(model: str | None) -> bool:
    if not model:
        return False
    return "image" in str(model).lower()


def _extract_data_url_base64(data_url: str) -> str | None:
    if not data_url or not isinstance(data_url, str):
        return None
    marker = "base64,"
    if marker not in data_url:
        return None
    return data_url.split(marker, 1)[1] or None


def _openai_messages_to_gemini_contents(messages: list[Any]) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    if not isinstance(messages, list):
        return contents

    for message in messages:
        if not isinstance(message, dict):
            continue

        role = message.get("role") or "user"
        raw_content = message.get("content")
        parts: list[dict[str, Any]] = []

        if isinstance(raw_content, str):
            if raw_content:
                parts.append({"text": raw_content})
        elif isinstance(raw_content, list):
            for content_part in raw_content:
                if not isinstance(content_part, dict):
                    continue

                part_type = str(content_part.get("type") or "").strip().lower()

                if part_type == "text":
                    text = content_part.get("text") or ""
                    if text:
                        parts.append({"text": text})
                    continue

                if part_type in {"image", "image_url", "input_image"}:
                    image_url = content_part.get("image_url") or content_part.get("image") or {}
                    data_url = None
                    if isinstance(image_url, dict):
                        data_url = image_url.get("url") or image_url.get("image_url")
                    elif isinstance(image_url, str):
                        data_url = image_url

                    if not data_url:
                        continue

                    b64 = _extract_data_url_base64(data_url)
                    if b64:
                        parts.append({
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": b64,
                            }
                        })
                    else:
                        parts.append({"file_data": {"file_uri": data_url}})
                    continue

                text = content_part.get("text")
                if text:
                    parts.append({"text": text})

        if not parts:
            continue

        gemini_role = "model" if role == "assistant" else "user"
        contents.append({"role": gemini_role, "parts": parts})

    return contents


def _to_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


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

        try:
            request_json = json.loads(body) if body else {}
        except json.JSONDecodeError:
            request_json = {}

        model_name = request_json.get("model")
        extra_body = request_json.get("extra_body") or {}
        google_extra = extra_body.get("google") or {}
        response_modalities = _to_list(
            google_extra.get("response_modalities")
            or google_extra.get("responseModalities")
        )
        generation_config = (
            request_json.get("generationConfig")
            or request_json.get("generation_config")
            or {}
        )
        response_modalities += _to_list(
            generation_config.get("responseModalities")
            or generation_config.get("response_modalities")
        )
        response_modalities_upper = {str(item).upper() for item in response_modalities}
        is_image_request = _is_image_model(model_name) or ("IMAGE" in response_modalities_upper)

        if is_image_request:
            if not model_name:
                raise HTTPException(status_code=400, detail="Missing 'model' for image request")

            model_id = str(model_name)
            if model_id.startswith("models/"):
                model_id = model_id[len("models/") :]

            if "contents" in request_json:
                gemini_payload = dict(request_json)
                gemini_payload.pop("model", None)
                gemini_payload.pop("extra_body", None)
            else:
                gemini_payload: dict[str, Any] = {
                    "contents": _openai_messages_to_gemini_contents(
                        request_json.get("messages", [])
                    )
                }

                image_generation_config: dict[str, Any] = {}
                if response_modalities:
                    image_generation_config["responseModalities"] = response_modalities

                image_config = google_extra.get("image_config") or google_extra.get("imageConfig")
                if image_config:
                    image_generation_config["imageConfig"] = image_config

                if "temperature" in request_json:
                    image_generation_config["temperature"] = request_json.get("temperature")

                if "max_tokens" in request_json:
                    image_generation_config["maxOutputTokens"] = request_json.get("max_tokens")

                if image_generation_config:
                    gemini_payload["generationConfig"] = image_generation_config

                if request_json.get("tools"):
                    gemini_payload["tools"] = request_json.get("tools")

            response = await http_client.post(
                f"{GEMINI_SDK_URL.rstrip('/')}/models/{model_id}:generateContent",
                json=gemini_payload,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                },
            )
        else:
            response = await http_client.post(
                f"{GEMINI_BASE_URL}/chat/completions",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )

        duration_ms = (datetime.now() - start_time).total_seconds() * 1000
        try:
            response_json = response.json()
        except Exception:
            response_json = None

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

        if response_json is None:
            return JSONResponse(content={"error": response.text}, status_code=response.status_code)
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
