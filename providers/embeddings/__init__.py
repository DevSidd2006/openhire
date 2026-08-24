"""
Embedding provider factory and implementations.
"""
from config.settings import EMBEDDING_PROVIDER, EMBEDDING_MODEL
from providers.base import EmbeddingProvider
from providers.embeddings.local import LocalEmbeddingProvider
from providers.embeddings.mock import MockEmbeddingProvider


def get_embedding_provider() -> EmbeddingProvider:
    """Factory function to get embedding provider based on configuration."""
    if EMBEDDING_PROVIDER == "local":
        return LocalEmbeddingProvider(model_name=EMBEDDING_MODEL)
    elif EMBEDDING_PROVIDER == "mock":
        return MockEmbeddingProvider(dimension=384)
    else:
        raise ValueError(f"Unknown embedding provider: {EMBEDDING_PROVIDER}")


__all__ = ["get_embedding_provider", "EmbeddingProvider", "LocalEmbeddingProvider", "MockEmbeddingProvider"]
