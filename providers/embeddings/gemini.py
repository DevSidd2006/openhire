"""
Gemini embedding provider (Google `google-genai` SDK).
"""
from providers.base import EmbeddingProvider

# gemini-embedding-001 (the only Gemini embedding model verified against
# this account/key - text-embedding-004 and gemini-embedding-exp-03-07
# both 404) returns 3072-dimensional vectors.
_DIMENSION_BY_MODEL = {
    "gemini-embedding-001": 3072,
}
_DEFAULT_DIMENSION = 3072


class GeminiEmbeddingProvider(EmbeddingProvider):
    """Embeddings via the Gemini API (`google-genai` SDK)."""

    def __init__(self, api_key: str, model: str = "gemini-embedding-001"):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for GeminiEmbeddingProvider")
        self.model = model
        self._embedding_dimension = _DIMENSION_BY_MODEL.get(model, _DEFAULT_DIMENSION)

        try:
            from google import genai
            self.client = genai.Client(api_key=api_key)
        except ImportError:
            raise ImportError(
                "google-genai package required for GeminiEmbeddingProvider. "
                "Install with: pip install google-genai"
            )

    async def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text using Gemini."""
        response = await self.client.aio.models.embed_content(model=self.model, contents=text)
        return list(response.embeddings[0].values)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts using Gemini."""
        response = await self.client.aio.models.embed_content(model=self.model, contents=texts)
        return [list(e.values) for e in response.embeddings]

    def get_embedding_dimension(self) -> int:
        """Get the dimension of embeddings produced by this provider's model."""
        return self._embedding_dimension
