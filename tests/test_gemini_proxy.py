from app.services import gemini_proxy
from app.services.gemini_proxy import _build_litellm_kwargs


def test_vertex_tools_are_sanitized_like_gemini() -> None:
    kwargs = _build_litellm_kwargs(
        {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "approach",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "target": {"type": "string"},
                                "location": {"type": "string"},
                            },
                            "required": ["target"],
                            "anyOf": [
                                {"required": ["target"]},
                                {"required": ["location"]},
                            ],
                        },
                    },
                }
            ]
        },
        litellm_model="vertex_ai/gemini-3.1-flash-lite",
        provider="vertex_ai",
        api_key=None,
        api_base=None,
        api_version=None,
    )

    parameters = kwargs["tools"][0]["function"]["parameters"]
    assert parameters["type"] == "object"
    assert "anyOf" not in parameters


def test_server_vertex_uses_api_key_and_native_vertex_url(monkeypatch) -> None:
    vertex_url = (
        "https://aiplatform.googleapis.com/v1/projects/pawfishing/"
        "locations/global/publishers/google"
    )
    monkeypatch.setattr(gemini_proxy, "GEMINI_API_KEY", "vertex-api-key")
    monkeypatch.setattr(gemini_proxy, "GEMINI_SDK_URL", vertex_url)

    assert gemini_proxy._provider_api_key("vertex_ai") == "vertex-api-key"
    assert gemini_proxy._resolve_litellm_transport(
        "vertex_ai/gemini-3-flash-preview",
        "vertex_ai",
        None,
    ) == (
        "gemini/gemini-3-flash-preview",
        "gemini",
        vertex_url,
    )


def test_client_vertex_uses_api_key_transport_and_custom_url() -> None:
    assert gemini_proxy._resolve_litellm_transport(
        "vertex_ai/gemini-3-flash-preview",
        "vertex_ai",
        "https://client.example.test",
    ) == (
        "gemini/gemini-3-flash-preview",
        "gemini",
        "https://client.example.test",
    )
