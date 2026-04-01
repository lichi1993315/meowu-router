import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.config import GEMINI_API_KEY, GEMINI_BASE_URL, GEMINI_SDK_URL
from app.core.logging import log
from app.services import sessions
from app.utils.crypto import decrypt_payload

_FINISH_REASON_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "OTHER": "stop",
}


def _gemini_response_to_openai(response_json: dict, model: str) -> dict:
    """Convert native Gemini generateContent response to OpenAI chat completion format."""
    choices = []
    candidates = response_json.get("candidates") or []
    for i, candidate in enumerate(candidates):
        content = candidate.get("content") or {}
        parts = content.get("parts") or []
        text_parts = []
        tool_calls = []
        
        for p in parts:
            if "text" in p:
                text_parts.append(p["text"])
            elif "functionCall" in p:
                fc = p["functionCall"]
                name = fc.get("name", "")
                args = fc.get("args", {})
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args, ensure_ascii=False)
                    }
                })
                
        finish_reason_raw = candidate.get("finishReason", "STOP")
        finish_reason = _FINISH_REASON_MAP.get(finish_reason_raw, "stop")
        
        if tool_calls and finish_reason == "stop":
            finish_reason = "tool_calls"
            
        message = {
            "role": "assistant",
            "content": "".join(text_parts) if text_parts else None,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        choices.append({
            "index": i,
            "message": message,
            "finish_reason": finish_reason,
        })

    usage_meta = response_json.get("usageMetadata") or {}
    prompt_tokens = usage_meta.get("promptTokenCount", 0)
    completion_tokens = usage_meta.get("candidatesTokenCount", 0)

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(datetime.now().timestamp()),
        "model": model,
        "choices": choices,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


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

        if role == "assistant" and "tool_calls" in message:
            for tc in message.get("tool_calls", []):
                if tc.get("type") == "function" and "function" in tc:
                    func = tc["function"]
                    try:
                        args = json.loads(func.get("arguments", "{}"))
                    except Exception:
                        args = {}
                    parts.append({
                        "functionCall": {
                            "name": func.get("name", ""),
                            "args": args
                        }
                    })

        if role == "tool":
            name = message.get("name") or "unknown_function"
            try:
                resp_json = json.loads(message.get("content", "{}") or "{}")
            except Exception:
                resp_json = {"result": message.get("content", "")}
                
            parts.append({
                "functionResponse": {
                    "name": name,
                    "response": {"name": name, "content": resp_json}
                }
            })

        if not parts:
            continue

        if role == "assistant":
            gemini_role = "model"
        else:
            gemini_role = "user"
            
        contents.append({"role": gemini_role, "parts": parts})

    return contents


def _sanitize_schema(schema: Any, is_properties_dict: bool = False) -> Any:
    if not isinstance(schema, dict):
        return schema
    
    sanitized = {}
    
    if is_properties_dict:
        for k, v in schema.items():
            sanitized[k] = _sanitize_schema(v, is_properties_dict=False)
        return sanitized
    
    # Google Gemini FunctionDeclaration Schema object only supports these exact fields.
    # Any other OpenAPI fields (like anyOf, minItems, etc.) will cause a 400 Bad Request.
    allowed_keys = {
        "type", "format", "description", "nullable", 
        "enum", "properties", "required", "items",
        "minimum", "maximum", "minLength", "maxLength",
        "pattern", "minItems", "maxItems", "minProperties", "maxProperties",
        "title", "default", "example", "propertyOrdering"
    }
    
    for k, v in schema.items():
        if k not in allowed_keys:
            continue
            
        if k == "properties" and isinstance(v, dict):
            sanitized[k] = _sanitize_schema(v, is_properties_dict=True)
        elif isinstance(v, dict):
            sanitized[k] = _sanitize_schema(v, is_properties_dict=False)
        elif isinstance(v, list):
            sanitized[k] = [_sanitize_schema(item, is_properties_dict=False) if isinstance(item, dict) else item for item in v]
        else:
            sanitized[k] = v
    return sanitized

def _openai_tools_to_gemini(tools: list[Any]) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    
    function_declarations = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and "function" in tool:
            func_def = dict(tool["function"])
            if "parameters" in func_def:
                func_def["parameters"] = _sanitize_schema(func_def["parameters"])
            function_declarations.append(func_def)
            
    if function_declarations:
        return [{"functionDeclarations": function_declarations}]
    return []


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

        model_name = request_json.get("model") or "gemini-3.0-flash"
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

        model_id = str(model_name)
        if model_id.startswith("models/"):
            model_id = model_id[len("models/") :]

        # Build Gemini-native payload from OpenAI-format request
        if "contents" in request_json:
            gemini_payload = dict(request_json)
            gemini_payload.pop("model", None)
            gemini_payload.pop("extra_body", None)
        else:
            messages = request_json.get("messages", [])
            system_messages = [m for m in messages if m.get("role") == "system"]
            user_and_model_messages = [m for m in messages if m.get("role") != "system"]
            gemini_payload: dict[str, Any] = {
                "contents": _openai_messages_to_gemini_contents(user_and_model_messages)
            }
            if system_messages:
                system_text_parts = []
                for sm in system_messages:
                    content = sm.get("content")
                    if isinstance(content, str):
                        system_text_parts.append(content)
                    elif isinstance(content, list):
                        for p in content:
                            if isinstance(p, dict) and p.get("type") == "text":
                                system_text_parts.append(p.get("text", ""))
                if system_text_parts:
                    gemini_payload["systemInstruction"] = {
                        "role": "user",
                        "parts": [{"text": "\n\n".join(system_text_parts)}]
                    }

        gen_config: dict[str, Any] = {}

        if is_image_request:
            if response_modalities:
                gen_config["responseModalities"] = response_modalities

            image_config = google_extra.get("image_config") or google_extra.get("imageConfig")
            if image_config:
                gen_config["imageConfig"] = image_config

        if "temperature" in request_json:
            gen_config["temperature"] = request_json["temperature"]

        if "max_tokens" in request_json:
            gen_config["maxOutputTokens"] = request_json["max_tokens"]

        if "top_p" in request_json:
            gen_config["topP"] = request_json["top_p"]

        if gen_config:
            gemini_payload["generationConfig"] = gen_config

        if request_json.get("tools"):
            gemini_tools = _openai_tools_to_gemini(request_json["tools"])
            if gemini_tools:
                gemini_payload["tools"] = gemini_tools

        base_url = GEMINI_SDK_URL.rstrip("/")
        response = await http_client.post(
            f"{base_url}/models/{model_id}:generateContent?key={api_key}",
            json=gemini_payload,
            headers={"Content-Type": "application/json"},
        )

        duration_ms = (datetime.now() - start_time).total_seconds() * 1000
        try:
            response_json = response.json()
        except Exception:
            response_json = None

        # Convert native Gemini response to OpenAI format for non-image requests
        if response_json is not None and response.is_success and not is_image_request:
            response_json = _gemini_response_to_openai(response_json, model_name)

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

        # Use query-param auth (key=) instead of header auth (x-goog-api-key)
        # because header-based auth hangs from this server
        separator = "&" if "?" in endpoint else "?"
        endpoint_with_key = f"{endpoint}{separator}key={api_key}"
        response = await http_client.post(
            endpoint_with_key,
            json=payload,
            headers={"Content-Type": "application/json"},
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
