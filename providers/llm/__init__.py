"""
LLM provider factory and implementations.
"""
from config.settings import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GROQ_API_KEY,
    GROQ_MODEL,
    LLM_PROVIDER,
    NVIDIA_NIM_API_KEY,
    NVIDIA_NIM_BASE_URL,
    NVIDIA_NIM_ENABLE_THINKING,
    NVIDIA_NIM_MODEL,
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
    "NvidiaNimProvider",
]
