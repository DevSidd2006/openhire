"""
Base provider interfaces.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> str:
        """Generate text from a prompt."""
        pass

    @abstractmethod
    async def generate_structured(
        self, prompt: str, schema: Dict[str, Any], **kwargs
    ) -> Dict[str, Any]:
        """Generate structured output matching a schema."""
        pass


class EmbeddingProvider(ABC):
    """Abstract base class for embedding providers."""

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Generate embedding for text."""
        pass

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        pass

    @abstractmethod
    def get_embedding_dimension(self) -> int:
        """Get the dimension of embeddings."""
        pass


class VectorStore(ABC):
    """Abstract base class for vector stores."""

    @abstractmethod
    async def add(self, ids: list[str], texts: list[str], embeddings: list[list[float]]) -> None:
        """Add items to the store."""
        pass

    @abstractmethod
    async def search(self, query_embedding: list[float], top_k: int = 5) -> list[Dict[str, Any]]:
        """Search for similar items."""
        pass

    @abstractmethod
    async def delete(self, ids: list[str]) -> None:
        """Delete items from the store."""
        pass

    @abstractmethod
    async def get(self, ids: list[str]) -> list[Dict[str, Any]]:
        """Retrieve items by ID."""
        pass

    @abstractmethod
    async def clear(self) -> None:
        """Clear the entire store."""
        pass


class AudioProcessor(ABC):
    """Abstract base class for audio processing."""

    @abstractmethod
    async def transcribe(self, audio_data: bytes, format: str = "wav") -> str:
        """Transcribe audio to text."""
        pass

    @abstractmethod
    async def extract_features(self, audio_data: bytes) -> Dict[str, Any]:
        """Extract audio features."""
        pass
