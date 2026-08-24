"""
FAISS-based vector store implementation.
"""
from typing import Any, Dict, List, Optional
import json
import os

from providers.base import VectorStore


class FAISSVectorStore(VectorStore):
    """FAISS vector store for similarity search."""

    def __init__(self, persist_dir: str, dimension: int = 384):
        self.persist_dir = persist_dir
        self.dimension = dimension
        self.index_path = os.path.join(persist_dir, "index.faiss")
        self.metadata_path = os.path.join(persist_dir, "metadata.json")

        os.makedirs(persist_dir, exist_ok=True)

        try:
            import faiss
            self.faiss = faiss
        except ImportError:
            raise ImportError("faiss-cpu required for FAISSVectorStore. Install with: pip install faiss-cpu")

        # Load or create index
        if os.path.exists(self.index_path):
            self.index = faiss.read_index(self.index_path)
            with open(self.metadata_path, "r") as f:
                self.metadata = json.load(f)
        else:
            self.index = faiss.IndexFlatL2(dimension)
            self.metadata = {}

    async def add(self, ids: List[str], texts: List[str], embeddings: List[List[float]]) -> None:
        """Add items to the store."""
        import numpy as np

        # Convert to numpy array
        embeddings_array = np.array(embeddings, dtype=np.float32)

        # Add to FAISS index
        self.index.add(embeddings_array)

        # Store metadata
        for id_, text in zip(ids, texts):
            self.metadata[id_] = {"text": text, "index": self.index.ntotal - len(ids) + list(ids).index(id_)}

        self._persist()

    async def search(self, query_embedding: List[float], top_k: int = 5) -> List[Dict[str, Any]]:
        """Search for similar items."""
        import numpy as np

        if self.index.ntotal == 0:
            return []

        query_array = np.array([query_embedding], dtype=np.float32)
        distances, indices = self.index.search(query_array, min(top_k, self.index.ntotal))

        results = []
        for idx, distance in zip(indices[0], distances[0]):
            # Find metadata for this index
            for id_, meta in self.metadata.items():
                if meta.get("index") == int(idx):
                    results.append({"id": id_, "text": meta["text"], "distance": float(distance)})
                    break

        return results

    async def delete(self, ids: List[str]) -> None:
        """Delete items from the store."""
        # Note: FAISS doesn't support deletion, so we'd need to rebuild
        # For now, just remove from metadata
        for id_ in ids:
            if id_ in self.metadata:
                del self.metadata[id_]
        self._persist()

    async def get(self, ids: List[str]) -> List[Dict[str, Any]]:
        """Retrieve items by ID."""
        results = []
        for id_ in ids:
            if id_ in self.metadata:
                results.append({"id": id_, **self.metadata[id_]})
        return results

    async def clear(self) -> None:
        """Clear the entire store."""
        import faiss

        self.index = faiss.IndexFlatL2(self.dimension)
        self.metadata = {}
        self._persist()

    def _persist(self) -> None:
        """Persist index and metadata to disk."""
        self.faiss.write_index(self.index, self.index_path)
        with open(self.metadata_path, "w") as f:
            json.dump(self.metadata, f)
