"""
LLM provider factory and implementations.
"""
from config.settings import LLM_PROVIDER, OPENAI_API_KEY, OPENAI_MODEL
from providers.base import LLMProvider
from providers.llm.mock import MockLLMProvider
from providers.llm.openai import OpenAIProvider


def get_llm_provider() -> LLMProvider:
    """Factory function to get LLM provider based on configuration."""
    if LLM_PROVIDER == "openai":
        if not OPENAI_API_KEY:
            raise ValueError(
                "OPENAI_API_KEY environment variable is required for OpenAI provider"
            )
        return OpenAIProvider(api_key=OPENAI_API_KEY, model=OPENAI_MODEL)
    elif LLM_PROVIDER == "mock":
        return MockLLMProvider()
    else:
        raise ValueError(f"Unknown LLM provider: {LLM_PROVIDER}")


__all__ = ["get_llm_provider", "LLMProvider", "MockLLMProvider", "OpenAIProvider"]
