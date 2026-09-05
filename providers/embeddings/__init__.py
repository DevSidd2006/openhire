"""
Embedding provider factory and implementations.
"""
from config.settings import (
    EMBEDDING_PROVIDER,
    EMBEDDING_MODEL,
    GEMINI_API_KEY,
    NVIDIA_NIM_API_KEY,
    NVIDIA_NIM_BASE_URL,
)
from providers.base import EmbeddingProvider
from providers.embeddings.gemini import GeminiEmbeddingProvider
from providers.embeddings.local import LocalEmbeddingProvider
from providers.embeddings.mock import MockEmbeddingProvider
from providers.embeddings.nvidia_nim import NvidiaNimEmbeddingProvider


def get_embedding_provider() -> EmbeddingProvider:
    """Factory function to get embedding provider based on configuration."""
    if EMBEDDING_PROVIDER == "nvidia-nim" or EMBEDDING_PROVIDER == "nvidia_nim":
        if not NVIDIA_NIM_API_KEY:
            raise ValueError(
                "NVIDIA_NIM_API_KEY environment variable is required for NVIDIA NIM embedding provider"
            )
        return NvidiaNimEmbeddingProvider(
            api_key=NVIDIA_NIM_API_KEY,
            model=EMBEDDING_MODEL or "nvidia/nv-embed-v2",
            base_url=NVIDIA_NIM_BASE_URL
        )
    elif EMBEDDING_PROVIDER == "gemini":
        if not GEMINI_API_KEY:
            raise ValueError(
                "GEMINI_API_KEY environment variable is required for Gemini embedding provider"
            )
        return GeminiEmbeddingProvider(
            api_key=GEMINI_API_KEY,
            model=EMBEDDING_MODEL or "gemini-embedding-001",
        )
    elif EMBEDDING_PROVIDER == "local":
        return LocalEmbeddingProvider(model_name=EMBEDDING_MODEL)
    elif EMBEDDING_PROVIDER == "mock":
        return MockEmbeddingProvider(dimension=384)
    else:
        raise ValueError(f"Unknown embedding provider: {EMBEDDING_PROVIDER}")


__all__ = [
    "get_embedding_provider",
    "EmbeddingProvider",
    "GeminiEmbeddingProvider",
    "LocalEmbeddingProvider",
    "MockEmbeddingProvider",
    "NvidiaNimEmbeddingProvider",
]
