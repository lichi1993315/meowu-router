import asyncio
import json
import re
import uuid
from datetime import datetime
from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.config import (
    DEFAULT_LLM_MODEL,
    GEMINI_API_KEY,
    GEMINI_IMAGE_MODEL,
    GEMINI_SDK_URL,
)
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

_API_KEY_FIELDS = (
    "api_key",
    "apiKey",
    "provider_api_key",
    "providerApiKey",
    "llm_api_key",
    "llmApiKey",
)
_API_BASE_FIELDS = ("api_base", "apiBase", "base_url", "baseUrl", "baseURL")
_MODEL_TYPE_FIELDS = (
    "model_type",
    "modelType",
    "provider",
    "llm_provider",
    "llmProvider",
)
_SERVER_ONLY_FIELDS = set(_API_KEY_FIELDS + _API_BASE_FIELDS + _MODEL_TYPE_FIELDS)
_SERVER_ONLY_FIELDS.update({"api_version", "apiVersion"})

_PROVIDER_ALIASES = {
    "anthropic": "anthropic",
    "claude": "anthropic",
    "deepseek": "deepseek",
    "doubao": "volcengine",
    "gemini": "gemini",
    "google": "gemini",
    "openai": "openai",
    "volcano": "volcengine",
    "volcengine": "volcengine",
    "ark": "volcengine",
}
_LITELLM_PROVIDER_PREFIXES = {
    "anthropic",
    "azure",
    "bedrock",
    "deepseek",
    "gemini",
    "huggingface",
    "ollama",
    "openai",
    "openai-compatible",
    "openrouter",
    "vertex_ai",
    "volcengine",
}
_OPENAI_MODEL_RE = re.compile(r"^(gpt-|o[1-9]|chatgpt-|text-|dall-e|tts-)", re.I)


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
                tool_call = {
                    "id": f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args, ensure_ascii=False)
                    }
                }
                thought_signature = _extract_function_call_thought_signature(fc, p)
                if thought_signature:
                    tool_call["extra_content"] = {
                        "google": {"thought_signature": thought_signature}
                    }
                tool_calls.append(tool_call)
                
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


def _clean_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value or value.lower() in {"none", "null", "undefined"}:
        return None
    return value


def _extract_body_field(request_json: dict[str, Any], field_names: tuple[str, ...]) -> str | None:
    for field_name in field_names:
        value = _clean_optional_str(request_json.get(field_name))
        if value:
            return value

    for parent_key in ("llm", "router", "provider_options", "providerOptions"):
        nested = request_json.get(parent_key)
        if not isinstance(nested, dict):
            continue
        for field_name in field_names:
            value = _clean_optional_str(nested.get(field_name))
            if value:
                return value
    return None


def _extract_bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    prefix = "bearer "
    if auth.lower().startswith(prefix):
        return _clean_optional_str(auth[len(prefix):])
    return _clean_optional_str(auth)


def _extract_client_api_key(request: Request, request_json: dict[str, Any]) -> str | None:
    body_key = _extract_body_field(request_json, _API_KEY_FIELDS)
    if body_key:
        return body_key
    for header_name in ("x-api-key", "api-key"):
        header_key = _clean_optional_str(request.headers.get(header_name))
        if header_key:
            return header_key
    return _extract_bearer_token(request)


def _normalize_provider(provider: str | None) -> str | None:
    provider = _clean_optional_str(provider)
    if not provider:
        return None
    return _PROVIDER_ALIASES.get(provider.lower(), provider.lower())


def _is_litellm_prefixed_model(model: str) -> bool:
    if "/" not in model:
        return False
    prefix = model.split("/", 1)[0].lower()
    return prefix in _LITELLM_PROVIDER_PREFIXES or prefix in _PROVIDER_ALIASES


def _strip_litellm_provider_prefix(model: str) -> str:
    if not _is_litellm_prefixed_model(model):
        return model
    return model.split("/", 1)[1]


def _infer_provider_from_model(model: str | None) -> str | None:
    model = _clean_optional_str(model)
    if not model:
        return None
    lowered = model.lower()
    if "/" in lowered:
        return _normalize_provider(lowered.split("/", 1)[0])
    if lowered.startswith("gemini"):
        return "gemini"
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith("deepseek"):
        return "deepseek"
    if lowered.startswith("doubao") or lowered.startswith("ep-"):
        return "volcengine"
    if _OPENAI_MODEL_RE.match(lowered):
        return "openai"
    return None


def _resolve_litellm_model(model: str | None, provider: str | None) -> tuple[str, str]:
    model = _clean_optional_str(model) or DEFAULT_LLM_MODEL
    provider = _normalize_provider(provider) or _infer_provider_from_model(model) or "gemini"

    if _is_litellm_prefixed_model(model):
        return model, provider
    return f"{provider}/{model}", provider


def _resolve_gemini_image_model(model: str | None, provider: str | None) -> str:
    model = _clean_optional_str(model)
    provider = _normalize_provider(provider) or _infer_provider_from_model(model)
    if model and (provider in (None, "gemini")):
        return _strip_litellm_provider_prefix(model)
    return GEMINI_IMAGE_MODEL


def _get_google_extra_body(request_json: dict[str, Any]) -> dict[str, Any]:
    extra_body = request_json.get("extra_body") or {}
    if not isinstance(extra_body, dict):
        return {}
    google_extra = extra_body.get("google") or {}
    return google_extra if isinstance(google_extra, dict) else {}


def _is_image_request(request_json: dict[str, Any], model: str | None) -> bool:
    google_extra = _get_google_extra_body(request_json)
    generation_config = (
        request_json.get("generationConfig")
        or request_json.get("generation_config")
        or {}
    )
    response_modalities = _to_list(
        google_extra.get("response_modalities")
        or google_extra.get("responseModalities")
    )
    if isinstance(generation_config, dict):
        response_modalities += _to_list(
            generation_config.get("responseModalities")
            or generation_config.get("response_modalities")
        )
    response_modalities_upper = {str(item).upper() for item in response_modalities}
    return _is_image_model(model) or ("IMAGE" in response_modalities_upper)


def _extract_api_base(request_json: dict[str, Any]) -> str | None:
    return _extract_body_field(request_json, _API_BASE_FIELDS)


def _extract_api_version(request_json: dict[str, Any]) -> str | None:
    return _clean_optional_str(request_json.get("api_version") or request_json.get("apiVersion"))


def _google_thinking_to_litellm(request_json: dict[str, Any]) -> dict[str, Any] | None:
    google_extra = _get_google_extra_body(request_json)
    thinking_config = google_extra.get("thinking_config") or google_extra.get("thinkingConfig")
    if not isinstance(thinking_config, dict):
        return None
    budget = (
        thinking_config.get("thinking_budget")
        if "thinking_budget" in thinking_config
        else thinking_config.get("thinkingBudget")
    )
    if budget is None:
        return None
    try:
        budget_int = int(budget)
    except (TypeError, ValueError):
        return None
    if budget_int <= 0:
        return {"type": "disabled"}
    return {"type": "enabled", "budget_tokens": budget_int}


def _build_litellm_kwargs(
    request_json: dict[str, Any],
    *,
    litellm_model: str,
    provider: str,
    api_key: str,
    api_base: str | None,
    api_version: str | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    for key, value in request_json.items():
        if key in _SERVER_ONLY_FIELDS:
            continue
        if key in {"model", "extra_body", "contents", "generationConfig", "generation_config"}:
            continue
        if key == "tools" and provider == "gemini":
            value = _sanitize_openai_tools_for_gemini(value)
        kwargs[key] = value

    kwargs["model"] = litellm_model
    kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base
    if api_version:
        kwargs["api_version"] = api_version

    if provider == "gemini" and "thinking" not in kwargs:
        thinking = _google_thinking_to_litellm(request_json)
        if thinking:
            kwargs["thinking"] = thinking

    # This router has never supported SSE streaming; keep behavior explicit.
    if kwargs.get("stream"):
        raise HTTPException(status_code=400, detail="Streaming is not supported by this router")

    # Let LiteLLM drop provider-unsupported OpenAI parameters instead of failing
    # during cross-provider model switches.
    kwargs.setdefault("drop_params", True)
    return kwargs


def _jsonable_response(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if hasattr(value, "dict"):
        return value.dict()
    return json.loads(json.dumps(value, default=str))


def _redact_secret_text(text: str, secret: str | None) -> str:
    if not secret:
        return text
    return text.replace(secret, "[REDACTED]")


async def _call_litellm_chat_completion(kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        from litellm import acompletion
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="litellm is not installed. Rebuild router-api after installing requirements.",
        ) from exc

    response = await acompletion(**kwargs)
    return _jsonable_response(response)


def _litellm_error_payload(exc: Exception, api_key: str | None) -> tuple[dict[str, Any], int]:
    status_code = getattr(exc, "status_code", None) or getattr(exc, "http_status", None) or 502
    try:
        status_code = int(status_code)
    except (TypeError, ValueError):
        status_code = 502
    if status_code < 400 or status_code > 599:
        status_code = 502

    message = _redact_secret_text(str(exc), api_key)
    payload = {
        "error": {
            "message": message,
            "type": exc.__class__.__name__,
            "param": getattr(exc, "param", None),
            "code": getattr(exc, "code", None),
        }
    }
    return payload, status_code


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


def _extract_function_call_thought_signature(
    function_call: dict[str, Any],
    part: dict[str, Any] | None = None,
) -> str | None:
    if not isinstance(function_call, dict):
        return None

    if isinstance(part, dict):
        part_signature = part.get("thoughtSignature") or part.get("thought_signature")
        if part_signature:
            return part_signature

    google_payload = function_call.get("google") or {}
    google_signature = (
        google_payload.get("thought_signature")
        or google_payload.get("thoughtSignature")
    )
    if google_signature:
        return google_signature

    direct_signature = (
        function_call.get("thought_signature")
        or function_call.get("thoughtSignature")
    )
    if direct_signature:
        return direct_signature

    return None


def _extract_tool_call_thought_signature(tool_call: dict[str, Any]) -> str | None:
    if not isinstance(tool_call, dict):
        return None

    extra_content_signature = (
        ((tool_call.get("extra_content") or {}).get("google") or {}).get("thought_signature")
    )
    if extra_content_signature:
        return extra_content_signature

    extra_content_signature_camel = (
        ((tool_call.get("extra_content") or {}).get("google") or {}).get("thoughtSignature")
    )
    if extra_content_signature_camel:
        return extra_content_signature_camel

    top_level_signature = tool_call.get("thought_signature") or tool_call.get("thoughtSignature")
    if top_level_signature:
        return top_level_signature

    function_signature = (
        ((tool_call.get("function") or {}).get("thought_signature"))
        or ((tool_call.get("function") or {}).get("thoughtSignature"))
    )
    if function_signature:
        return function_signature

    return None


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
                    function_call = {
                        "functionCall": {
                            "name": func.get("name", ""),
                            "args": args
                        }
                    }
                    thought_signature = _extract_tool_call_thought_signature(tc)
                    if thought_signature:
                        function_call["thoughtSignature"] = thought_signature
                    parts.append(function_call)

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


def _sanitize_openai_tools_for_gemini(tools: Any) -> Any:
    if not isinstance(tools, list):
        return tools

    sanitized_tools = []
    for tool in tools:
        if not isinstance(tool, dict):
            sanitized_tools.append(tool)
            continue

        sanitized_tool = dict(tool)
        function_def = sanitized_tool.get("function")
        if tool.get("type") == "function" and isinstance(function_def, dict):
            sanitized_function = dict(function_def)
            if "parameters" in sanitized_function:
                sanitized_function["parameters"] = _sanitize_schema(
                    sanitized_function["parameters"]
                )
            sanitized_tool["function"] = sanitized_function
        sanitized_tools.append(sanitized_tool)
    return sanitized_tools


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

        requested_model = _clean_optional_str(request_json.get("model"))
        requested_provider = _extract_body_field(request_json, _MODEL_TYPE_FIELDS)
        client_api_key = _extract_client_api_key(request, request_json)
        use_client_model = bool(requested_model and client_api_key)

        if _is_image_request(request_json, requested_model):
            api_key = GEMINI_API_KEY
            if not api_key:
                raise HTTPException(status_code=500, detail="GEMINI_API_KEY not set")

            model_name = _resolve_gemini_image_model(requested_model, requested_provider)
            google_extra = _get_google_extra_body(request_json)
            generation_config = (
                request_json.get("generationConfig")
                or request_json.get("generation_config")
                or {}
            )
            response_modalities = _to_list(
                google_extra.get("response_modalities")
                or google_extra.get("responseModalities")
            )
            if isinstance(generation_config, dict):
                response_modalities += _to_list(
                    generation_config.get("responseModalities")
                    or generation_config.get("response_modalities")
                )
            if not response_modalities:
                response_modalities = ["TEXT", "IMAGE"]

            model_id = str(model_name)
            if model_id.startswith("models/"):
                model_id = model_id[len("models/") :]
            model_id = _strip_litellm_provider_prefix(model_id)

            # Build Gemini-native payload from OpenAI-format request.
            if "contents" in request_json:
                gemini_payload = dict(request_json)
                gemini_payload.pop("model", None)
                gemini_payload.pop("extra_body", None)
                for key in _SERVER_ONLY_FIELDS:
                    gemini_payload.pop(key, None)
                for key in ("llm", "router", "provider_options", "providerOptions"):
                    gemini_payload.pop(key, None)
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
                            "parts": [{"text": "\n\n".join(system_text_parts)}],
                        }

            gen_config: dict[str, Any] = {}
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

        if use_client_model:
            litellm_model, provider = _resolve_litellm_model(requested_model, requested_provider)
            upstream_api_key = client_api_key
        else:
            upstream_api_key = GEMINI_API_KEY
            if not upstream_api_key:
                raise HTTPException(status_code=500, detail="GEMINI_API_KEY not set")
            litellm_model, provider = _resolve_litellm_model(DEFAULT_LLM_MODEL, "gemini")

        litellm_kwargs = _build_litellm_kwargs(
            request_json,
            litellm_model=litellm_model,
            provider=provider,
            api_key=upstream_api_key,
            api_base=_extract_api_base(request_json),
            api_version=_extract_api_version(request_json),
        )

        try:
            response_json = await _call_litellm_chat_completion(litellm_kwargs)
            response_status = 200
        except Exception as exc:
            response_json, response_status = _litellm_error_payload(exc, upstream_api_key)

        duration_ms = (datetime.now() - start_time).total_seconds() * 1000

        asyncio.create_task(
            sessions.save_response_to_file(
                response_json=response_json,
                response_status=response_status,
                duration_ms=duration_ms,
                user_id=user_id,
                filepath=filepath,
                timestamp_iso=timestamp_iso,
            )
        )

        return JSONResponse(content=response_json, status_code=response_status)
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
