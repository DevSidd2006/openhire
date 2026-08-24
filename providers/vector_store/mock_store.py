"""
Mock vector store for testing.
"""
from typing import Any, Dict, List
import json

from providers.base import VectorStore


class MockVectorStore(VectorStore):
    """In-memory mock vector store for testing."""

    def __init__(self):
        self.store: Dict[str, Dict[str, Any]] = {}

    async def add(self, ids: List[str], texts: List[str], embeddings: List[List[float]]) -> None:
        """Add items to the store."""
        for id_, text, embedding in zip(ids, texts, embeddings):
            self.store[id_] = {
                "id": id_,
                "text": text,
                "embedding": embedding,
            }

    async def search(self, query_embedding: List[float], top_k: int = 5) -> List[Dict[str, Any]]:
        """Search for similar items using simple cosine similarity."""
        if not self.store:
            return []

        # Calculate cosine similarity
        def cosine_similarity(a: List[float], b: List[float]) -> float:
            import math
            dot_product = sum(x * y for x, y in zip(a, b))
            norm_a = math.sqrt(sum(x * x for x in a))
            norm_b = math.sqrt(sum(x * x for x in b))
            if norm_a == 0 or norm_b == 0:
                return 0
            return dot_product / (norm_a * norm_b)

        # Score all items
        scores = []
        for id_, item in self.store.items():
            similarity = cosine_similarity(query_embedding, item["embedding"])
            scores.append({"id": id_, "text": item["text"], "similarity": similarity})

        # Sort by similarity and return top_k
        scores.sort(key=lambda x: x["similarity"], reverse=True)
        return scores[:top_k]

    async def delete(self, ids: List[str]) -> None:
        """Delete items from the store."""
        for id_ in ids:
            if id_ in self.store:
                del self.store[id_]

    async def get(self, ids: List[str]) -> List[Dict[str, Any]]:
        """Retrieve items by ID."""
        results = []
        for id_ in ids:
            if id_ in self.store:
                results.append(self.store[id_])
        return results

    async def clear(self) -> None:
        """Clear the entire store."""
        self.store.clear()
