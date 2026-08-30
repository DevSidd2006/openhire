"""
NVIDIA NIM embedding provider using OpenAI-compatible API.
"""
import os
from providers.base import EmbeddingProvider


class NvidiaNimEmbeddingProvider(EmbeddingProvider):
    """Embeddings via NVIDIA NIM API (OpenAI-compatible)."""

    def __init__(self, api_key: str, model: str = "nvidia/nemotron-3-embed-1b", base_url: str = None):
        """Initialize NVIDIA NIM embedding provider."""
        self.api_key = api_key
        self.model = model
        self.base_url = base_url or "https://integrate.api.nvidia.com/v1"
        self.client = None
        # nemotron-3-embed-1b produces 384-dimensional embeddings
        self._embedding_dimension = 384

    def _get_client(self):
        """Lazy-load OpenAI client for NVIDIA NIM."""
        if self.client is None:
            try:
                from openai import AsyncOpenAI
                self.client = AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url
                )
            except ImportError:
                raise ImportError(
                    "openai package required for NvidiaNimEmbeddingProvider. "
                    "Install with: pip install openai"
                )
        return self.client

    async def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text using NVIDIA NIM."""
        client = self._get_client()
        response = await client.embeddings.create(
            model=self.model,
            input=[text]
        )
        return response.data[0].embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts using NVIDIA NIM."""
        client = self._get_client()
        response = await client.embeddings.create(
            model=self.model,
            input=texts
        )
        # Sort by index to ensure consistent ordering
        embeddings = sorted(response.data, key=lambda x: x.index)
        return [embedding.embedding for embedding in embeddings]

    def get_embedding_dimension(self) -> int:
        """Get embedding dimension for NVIDIA NIM nv-embed-v2."""
        return self._embedding_dimension
