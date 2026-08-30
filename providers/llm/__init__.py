"""
LLM provider factory and implementations.
"""
from config.settings import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GROQ_API_KEY,
    GROQ_MODEL,
    LLM_PROVIDER,
    OPENAI_API_KEY,
    OPENAI_MODEL,
)
from providers.base import LLMProvider
from providers.llm.gemini import GeminiProvider
from providers.llm.groq import GroqProvider
from providers.llm.mock import MockLLMProvider
from providers.llm.nvidia_nim import NvidiaNimProvider
from providers.llm.openai import OpenAIProvider


def get_llm_provider() -> LLMProvider:
    """Factory function to get LLM provider based on configuration."""
    # NVIDIA NIM is the primary provider (Nemotron ultra-powerful with extended thinking).
    # Groq, OpenAI, and Gemini remain fully supported alternatives.
    if LLM_PROVIDER == "nvidia_nim" or LLM_PROVIDER == "nvidia-nim":
        import os
        api_key = os.getenv("NVIDIA_NIM_API_KEY", "")
        if not api_key:
            raise ValueError(
                "NVIDIA_NIM_API_KEY environment variable is required for NVIDIA NIM provider"
            )
        model = os.getenv("NVIDIA_NIM_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
        base_url = os.getenv("NVIDIA_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")
        return NvidiaNimProvider(api_key=api_key, model=model, base_url=base_url)
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
    elif LLM_PROVIDER == "mock":
        return MockLLMProvider()
    else:
        raise ValueError(f"Unknown LLM provider: {LLM_PROVIDER}")


__all__ = [
    "get_llm_provider",
    "LLMProvider",
    "MockLLMProvider",
    "OpenAIProvider",
    "GeminiProvider",
    "GroqProvider",
]
