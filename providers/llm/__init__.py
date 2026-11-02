"""
LLM provider factory and implementations.
"""
from typing import Optional

from config.settings import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    GROQ_API_KEY,
    GROQ_MODEL,
    LLM_PROVIDER,
    NVIDIA_NIM_API_KEY,
    NVIDIA_NIM_BASE_URL,
    NVIDIA_NIM_ENABLE_THINKING,
    NVIDIA_NIM_MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    OPENAI_API_KEY,
    OPENAI_MODEL,
)
from core.llm_context import get_context_provider
from providers.base import LLMPermanentError, LLMProvider, LLMTransientError
from providers.llm.gemini import GeminiProvider
from providers.llm.groq import GroqProvider
from providers.llm.mock import MockLLMProvider
from providers.llm.nvidia_nim import NvidiaNimProvider
from providers.llm.openai import OpenAIProvider


def get_llm_provider() -> LLMProvider:
    """Factory function to get LLM provider.

    Resolves in this order:
    1. A request-scoped BYOK provider set via core/llm_context.py's
       ContextVar (set by a FastAPI dependency on recruiter-authenticated
       routes - see api/dependencies for `apply_llm_context_for_recruiter`).
    2. The existing environment-variable dispatch below, unchanged - this
       is the ENTIRE fallback path main.py (the CLI) and every test that
       does not touch the ContextVar always takes.
    """
    context_provider = get_context_provider()
    if context_provider is not None:
        return context_provider

    # NVIDIA NIM is the primary provider (Nemotron; super-120b by default -
    # fastest and widest of the family, reasoning mode off. See
    # config/settings.py for the benchmark table behind that choice).
    # Groq, OpenAI, and Gemini remain fully supported alternatives.
    if LLM_PROVIDER == "nvidia_nim" or LLM_PROVIDER == "nvidia-nim":
        if not NVIDIA_NIM_API_KEY:
            raise ValueError(
                "NVIDIA_NIM_API_KEY environment variable is required for NVIDIA NIM provider"
            )
        return NvidiaNimProvider(
            api_key=NVIDIA_NIM_API_KEY,
            model=NVIDIA_NIM_MODEL,
            base_url=NVIDIA_NIM_BASE_URL,
            enable_thinking=NVIDIA_NIM_ENABLE_THINKING,
        )
    elif LLM_PROVIDER == "groq":
        if not GROQ_API_KEY:
            raise ValueError(
                "GROQ_API_KEY environment variable is required for Groq provider"
            )
        return GroqProvider(api_key=GROQ_API_KEY, model=GROQ_MODEL)
    elif LLM_PROVIDER == "openai":
        if not OPENAI_API_KEY:
            raise ValueError(
                "OPENAI_API_KEY environment variable is required for OpenAI provider"
            )
        return OpenAIProvider(api_key=OPENAI_API_KEY, model=OPENAI_MODEL)
    elif LLM_PROVIDER == "gemini":
        if not GEMINI_API_KEY:
            raise ValueError(
                "GEMINI_API_KEY environment variable is required for Gemini provider"
            )
        return GeminiProvider(api_key=GEMINI_API_KEY, model=GEMINI_MODEL)
    elif LLM_PROVIDER == "anthropic":
        if not ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY environment variable is required for Anthropic provider")
        return AnthropicProvider(api_key=ANTHROPIC_API_KEY, model=ANTHROPIC_MODEL)
    elif LLM_PROVIDER == "openrouter":
        if not OPENROUTER_API_KEY:
            raise ValueError("OPENROUTER_API_KEY environment variable is required for OpenRouter provider")
        return OpenAIProvider(api_key=OPENROUTER_API_KEY, model=OPENROUTER_MODEL, base_url="https://openrouter.ai/api/v1")
    elif LLM_PROVIDER == "mock":
        return MockLLMProvider()
    else:
        raise ValueError(f"Unknown LLM provider: {LLM_PROVIDER}")


def build_provider(name: str, api_key: str, model: Optional[str] = None) -> LLMProvider:
    """Construct a provider from explicit values. Reads no environment
    except each provider's own model-default constant when `model` is
    omitted - the key and provider name always come from the caller
    (BYOK), never from OPENAI_API_KEY/GROQ_API_KEY/etc.

    Returns the SAME four classes get_llm_provider() already uses, so a
    BYOK-driven agent gets identical error classification and
    structured-output handling to the system-key path - no parallel client.
    """
    if name == "openai":
        return OpenAIProvider(api_key=api_key, model=model or OPENAI_MODEL)
    if name == "gemini":
        return GeminiProvider(api_key=api_key, model=model or GEMINI_MODEL)
    if name == "groq":
        return GroqProvider(api_key=api_key, model=model or GROQ_MODEL)
    if name == "nvidia_nim":
        return NvidiaNimProvider(
            api_key=api_key,
            model=model or NVIDIA_NIM_MODEL,
            base_url=NVIDIA_NIM_BASE_URL,
            enable_thinking=NVIDIA_NIM_ENABLE_THINKING,
        )
    if name == "openrouter":
        return OpenAIProvider(api_key=api_key, model=model or "openai/gpt-4o-mini", base_url="https://openrouter.ai/api/v1")
    if name == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model or "claude-3-5-sonnet-latest")
    raise ValueError(
        f"Unknown BYOK provider: {name!r}. Supported: openai, gemini, anthropic, groq, nvidia_nim, openrouter."
    )


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API adapter; uses the shared async HTTP client library."""

    def __init__(self, api_key: str, model: str):
        self.api_key, self.model = api_key, model
        self.last_usage = {}

    async def _call(self, prompt: str, max_tokens: int) -> str:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post("https://api.anthropic.com/v1/messages", headers={
                    "x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"
                }, json={"model": self.model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]})
            if response.status_code >= 400:
                error = f"Anthropic request failed ({response.status_code})"
                if response.status_code == 429 or response.status_code >= 500:
                    from providers.base import LLMTransientError
                    raise LLMTransientError(error)
                from providers.base import LLMPermanentError
                raise LLMPermanentError(error)
            data = response.json()
            usage = data.get("usage") or {}
            self.last_usage = {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)}
            return "".join(part.get("text", "") for part in data.get("content", []))
        except (LLMPermanentError, LLMTransientError):
            raise
        except Exception as exc:
            raise LLMTransientError("Anthropic request could not be completed") from exc

    async def generate(self, prompt: str, **kwargs) -> str:
        return await self._call(prompt, kwargs.get("max_tokens", 2048))

    async def generate_structured(self, prompt: str, schema, **kwargs):
        import json
        from providers.base import LLMTransientError
        result = await self._call(prompt + "\nReturn only JSON conforming to this JSON Schema:\n" + json.dumps(schema), kwargs.get("max_tokens", 2048))
        try:
            return json.loads(result)
        except (ValueError, TypeError) as exc:
            raise LLMTransientError("Anthropic returned malformed structured JSON") from exc


__all__ = [
    "get_llm_provider",
    "build_provider",
    "LLMProvider",
    "MockLLMProvider",
    "OpenAIProvider",
    "GeminiProvider",
    "GroqProvider",
    "NvidiaNimProvider",
    "AnthropicProvider",
]
