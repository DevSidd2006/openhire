import httpx
import pytest

from services import provider_health


@pytest.mark.asyncio
async def test_model_metadata_check_verifies_model_without_generation(monkeypatch):
    captured = {}

    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, url, headers):
            captured.update(url=url, headers=headers)
            return httpx.Response(200, json={"data": [{"id": "gpt-test"}]})

    monkeypatch.setattr(provider_health.httpx, "AsyncClient", Client)
    result = await provider_health.check_provider("openai", "secret", "gpt-test")
    assert result["status"] == "connected"
    assert result["auth_status"] == "valid"
    assert result["last_successful_test_at"]
    assert captured["url"] == "https://api.openai.com/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,expected", [
    (401, {"error": "bad key"}, "invalid_api_key"),
    (403, {"error": "permission denied"}, "model_unavailable"),
    (429, {"error": "rate limit"}, "rate_limited"),
    (429, {"error": "insufficient_quota"}, "quota_exhausted"),
    (200, {"data": [{"id": "another-model"}]}, "model_unavailable"),
])
async def test_provider_health_categorizes_failures(monkeypatch, status, body, expected):
    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, *_args, **_kwargs): return httpx.Response(status, json=body)

    monkeypatch.setattr(provider_health.httpx, "AsyncClient", Client)
    result = await provider_health.check_provider("groq", "secret", "llama-test")
    assert result["status"] == expected
    assert result["connected"] is False
    assert result["last_tested_at"]


@pytest.mark.asyncio
async def test_gemini_uses_models_get_metadata_endpoint(monkeypatch):
    captured = {}

    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, url, headers):
            captured.update(url=url, headers=headers)
            return httpx.Response(200, json={"name": "models/gemini-test"})

    monkeypatch.setattr(provider_health.httpx, "AsyncClient", Client)
    result = await provider_health.check_provider("gemini", "secret", "gemini-test")
    assert result["connected"] is True
    assert captured["url"].endswith("/models/gemini-test")
    assert captured["headers"]["x-goog-api-key"] == "secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model,url_fragment,auth_header", [
    ("anthropic", "claude-test", "/v1/models?limit=100", "x-api-key"),
    ("groq", "llama-test", "api.groq.com/openai/v1/models", "Authorization"),
    ("nvidia_nim", "nim-test", "integrate.api.nvidia.com/v1/models", "Authorization"),
])
async def test_provider_specific_model_catalog_endpoints(monkeypatch, provider, model, url_fragment, auth_header):
    captured = {}

    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, url, headers):
            captured.update(url=url, headers=headers)
            return httpx.Response(200, json={"data": [{"id": model}]})

    monkeypatch.setattr(provider_health.httpx, "AsyncClient", Client)
    result = await provider_health.check_provider(provider, "secret", model)
    assert result["connected"] is True
    assert url_fragment in captured["url"]
    assert auth_header in captured["headers"]


@pytest.mark.asyncio
async def test_openrouter_verifies_key_then_model_catalog(monkeypatch):
    calls = []

    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, url, headers):
            calls.append(url)
            body = {"data": [{"id": "openai/gpt-4o-mini"}]} if url.endswith("/models") else {"data": {"label": "test"}}
            return httpx.Response(200, json=body)

    monkeypatch.setattr(provider_health.httpx, "AsyncClient", Client)
    result = await provider_health.check_provider("openrouter", "secret", "openai/gpt-4o-mini")
    assert result["connected"] is True
    assert calls == ["https://openrouter.ai/api/v1/auth/key", "https://openrouter.ai/api/v1/models"]
