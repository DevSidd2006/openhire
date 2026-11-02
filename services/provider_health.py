"""Lightweight authentication and model-access checks for supported LLM APIs."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx


PROVIDERS = ("openai", "gemini", "anthropic", "groq", "nvidia_nim", "openrouter")


def platform_credentials() -> dict[str, tuple[str, str, str]]:
    from config import settings as cfg
    return {
        "openai": (cfg.OPENAI_API_KEY, cfg.OPENAI_MODEL, "https://api.openai.com/v1"),
        "gemini": (cfg.GEMINI_API_KEY, cfg.GEMINI_MODEL, "https://generativelanguage.googleapis.com/v1beta"),
        "anthropic": (getattr(cfg, "ANTHROPIC_API_KEY", ""), getattr(cfg, "ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"), "https://api.anthropic.com/v1"),
        "groq": (cfg.GROQ_API_KEY, cfg.GROQ_MODEL, "https://api.groq.com/openai/v1"),
        "nvidia_nim": (cfg.NVIDIA_NIM_API_KEY, cfg.NVIDIA_NIM_MODEL, cfg.NVIDIA_NIM_BASE_URL.rstrip("/")),
        "openrouter": (getattr(cfg, "OPENROUTER_API_KEY", ""), getattr(cfg, "OPENROUTER_MODEL", "openai/gpt-4o-mini"), "https://openrouter.ai/api/v1"),
    }


def _request(provider: str, model: str, api_key: str, base: str):
    headers = {"Authorization": f"Bearer {api_key}"}
    if provider == "gemini":
        url = f"{base}/models/{quote(model.removeprefix('models/'), safe='-._')}"
        return url, {"x-goog-api-key": api_key}, None
    if provider == "anthropic":
        return f"{base}/models?limit=100", {"x-api-key": api_key, "anthropic-version": "2023-06-01"}, None
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1/auth/key", headers, None
    return f"{base}/models", headers, None


def _model_ids(provider: str, payload: dict) -> set[str]:
    values = payload.get("data") or payload.get("models") or []
    if provider == "gemini":
        return {str(item.get("name", "")).removeprefix("models/") for item in values}
    return {str(item.get("id", "")) for item in values}


async def check_provider(provider: str, api_key: str | None, model: str | None, *, timeout: float = 12) -> dict:
    """Only list/get model metadata: no generation, token spend, or prompt submission."""
    stamp = datetime.now(timezone.utc).isoformat()
    if provider not in PROVIDERS:
        return {"provider": provider, "model": model, "status": "connection_error", "auth_status": "unknown", "connected": False, "last_tested_at": stamp, "last_successful_test_at": None, "error_code": "unsupported_provider", "message": "Unsupported provider."}
    if not api_key:
        return {"provider": provider, "model": model, "status": "not_configured", "auth_status": "not_configured", "connected": False, "last_tested_at": None, "last_successful_test_at": None, "error_code": None, "message": "No API key is configured."}
    base = platform_credentials()[provider][2]
    model = model or platform_credentials()[provider][1]
    url, headers, _ = _request(provider, model, api_key, base)
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
        status_code = response.status_code
        data = response.json() if response.content else {}
        if status_code < 300:
            # Gemini's models.get endpoint itself validates the selected model.
            if provider == "openrouter":
                async with httpx.AsyncClient(timeout=timeout) as client:
                    models_response = await client.get("https://openrouter.ai/api/v1/models", headers=headers)
                available = models_response.status_code < 300 and model in _model_ids(provider, models_response.json())
            else:
                available = True if provider == "gemini" else model in _model_ids(provider, data)
            if available:
                return {"provider": provider, "model": model, "status": "connected", "auth_status": "valid", "connected": True, "last_tested_at": stamp, "last_successful_test_at": stamp, "error_code": None, "message": "Credential and model verified.", "latency_ms": max(1, int((time.monotonic()-started)*1000))}
            return {"provider": provider, "model": model, "status": "model_unavailable", "auth_status": "valid", "connected": False, "last_tested_at": stamp, "last_successful_test_at": None, "error_code": "model_unavailable", "message": "The key is valid, but the selected model is not available to this account."}
        body = response.text.lower()
        if status_code == 401:
            status, auth, code, msg = "invalid_api_key", "invalid", "invalid_api_key", "Provider rejected the API key."
        elif status_code == 403:
            status, auth, code, msg = "model_unavailable", "valid", "permission_denied", "The key lacks permission for this model or endpoint."
        elif status_code == 404:
            status, auth, code, msg = "model_unavailable", "valid", "model_unavailable", "The selected model was not found or is unavailable."
        elif status_code == 400 and any(word in body for word in ("model", "not found", "does not exist", "unsupported")):
            status, auth, code, msg = "model_unavailable", "valid", "model_unavailable", "The selected model is invalid or unavailable to this account."
        elif status_code == 429 and any(word in body for word in ("quota", "billing", "insufficient_quota", "exhausted")):
            status, auth, code, msg = "quota_exhausted", "valid", "quota_exhausted", "Provider quota or billing limit is exhausted."
        elif status_code == 429:
            status, auth, code, msg = "rate_limited", "valid", "rate_limited", "Provider rate limit reached; try again later."
        else:
            status, auth, code, msg = "connection_error", "unknown", "provider_error", f"Provider check failed (HTTP {status_code})."
    except (httpx.TimeoutException, httpx.ConnectError):
        status, auth, code, msg = "connection_error", "unknown", "connection_error", "Could not connect to the provider."
    except Exception:
        status, auth, code, msg = "connection_error", "unknown", "connection_error", "Provider health check failed."
    return {"provider": provider, "model": model, "status": status, "auth_status": auth, "connected": False, "last_tested_at": stamp, "last_successful_test_at": None, "error_code": code, "message": msg, "latency_ms": max(1, int((time.monotonic()-started)*1000))}
