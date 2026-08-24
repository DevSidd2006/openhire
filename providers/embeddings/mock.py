"""
Mock embedding provider for testing.
"""
import hashlib
from providers.base import EmbeddingProvider


class MockEmbeddingProvider(EmbeddingProvider):
    """Mock embedding provider that creates deterministic embeddings."""

    def __init__(self, dimension: int = 384):
        self.dimension = dimension

    async def embed(self, text: str) -> list[float]:
        """Generate deterministic embedding for a single text."""
        return self._generate_mock_embedding(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        return [self._generate_mock_embedding(text) for text in texts]

    def get_embedding_dimension(self) -> int:
        """Get embedding dimension."""
        return self.dimension

    def _generate_mock_embedding(self, text: str) -> list[float]:
        """Generate a deterministic mock embedding based on text hash."""
        # Create a hash-based seed for reproducibility
        hash_object = hashlib.md5(text.encode())
        hash_bytes = hash_object.digest()

        # Create embedding by spreading hash values
        embedding = []
        for i in range(self.dimension):
            byte_val = hash_bytes[i % len(hash_bytes)]
            # Normalize to [-1, 1]
            normalized = (byte_val / 127.5) - 1.0
            embedding.append(normalized)

        return embedding
