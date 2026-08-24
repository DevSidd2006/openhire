"""
Vector store factory and implementations.
"""
from config.settings import VECTOR_STORE_TYPE, VECTOR_STORE_PATH
from providers.base import VectorStore
from providers.vector_store.faiss_store import FAISSVectorStore
from providers.vector_store.mock_store import MockVectorStore


def get_vector_store() -> VectorStore:
    """Factory function to get vector store based on configuration."""
    if VECTOR_STORE_TYPE == "faiss":
        return FAISSVectorStore(persist_dir=VECTOR_STORE_PATH, dimension=384)
    elif VECTOR_STORE_TYPE == "mock":
        return MockVectorStore()
    else:
        raise ValueError(f"Unknown vector store type: {VECTOR_STORE_TYPE}")


__all__ = ["get_vector_store", "VectorStore", "FAISSVectorStore", "MockVectorStore"]
