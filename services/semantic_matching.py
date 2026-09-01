"""Semantic matching using embeddings for resume-to-job matching.

Provides span retrieval for the evidence-bound matcher, plus the legacy
skill/JD similarity helpers.

Failure policy: every embedding failure raises EmbeddingUnavailableError.
These methods previously returned 0.0 on any exception, which made a
provider outage indistinguishable from a genuinely terrible candidate and
therefore silently converted an outage into a wave of rejections. Callers
are expected to route the affected application to SCORING_PENDING - a
system failure must never look like a candidate failure.
"""
from typing import List, Sequence, Tuple
import math

from core.logging import get_logger
from providers import get_embedding_provider

logger = get_logger("services.semantic_matching")


class EmbeddingUnavailableError(Exception):
    """The embedding provider could not be reached or returned garbage.

    Raised instead of returning 0.0. A 0.0 similarity is indistinguishable
    from "this candidate is a terrible match", so swallowing an outage here
    would turn infrastructure trouble into hiring decisions.
    """


class SemanticMatcher:
    """Semantic matching using embeddings."""

    def __init__(self, embedding_provider=None):
        # Injectable so failure paths are testable; defaults to the
        # configured provider in normal operation.
        self.embedding_provider = embedding_provider or get_embedding_provider()

    async def retrieve_spans_for_competency(
        self, competency, spans: Sequence, k: int = 8
    ) -> List:
        """Return the `k` spans most semantically similar to `competency`.

        Deliberately generous: recall is won here, and a span that is
        retrieved but goes uncited in verification costs nothing, whereas a
        stingy k silently caps recall with no visible symptom.
        """
        if not spans:
            return []

        query = f"{competency.name}: {competency.definition}"
        try:
            embeddings = await self.embedding_provider.embed_batch(
                [query] + [s.text for s in spans]
            )
        except Exception as e:
            logger.error(f"Embedding provider failed during span retrieval: {e}")
            raise EmbeddingUnavailableError(str(e)) from e

        query_embedding, span_embeddings = embeddings[0], embeddings[1:]
        scored = [
            (self._cosine_similarity(query_embedding, emb), span)
            for emb, span in zip(span_embeddings, spans)
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [span for _, span in scored[:k]]

    async def calculate_skill_semantic_similarity(
        self, candidate_skills: List[str], required_skills: List[str], preferred_skills: List[str]
    ) -> Tuple[float, List[str]]:
        """Calculate semantic similarity between candidate skills and job requirements.

        Returns:
            (semantic_match_score, semantically_matched_skills)
        """
        if not required_skills or not candidate_skills:
            return 0.0, []

        try:
            all_skills = list(set(candidate_skills + required_skills + preferred_skills))
            skill_embeddings = await self.embedding_provider.embed_batch(all_skills)
        except Exception as e:
            logger.error(f"Embedding provider failed during skill matching: {e}")
            raise EmbeddingUnavailableError(str(e)) from e

        skill_to_embedding = dict(zip(all_skills, skill_embeddings))

        semantically_matched: List[str] = []
        match_scores: List[float] = []

        for req_skill in required_skills:
            req_embedding = skill_to_embedding[req_skill]
            best_similarity = 0.0

            for cand_skill in candidate_skills:
                cand_embedding = skill_to_embedding[cand_skill]
                similarity = self._cosine_similarity(req_embedding, cand_embedding)

                if similarity > best_similarity:
                    best_similarity = similarity

                if similarity > 0.7 and cand_skill not in semantically_matched:
                    semantically_matched.append(cand_skill)

            match_scores.append(best_similarity)

        semantic_score = sum(match_scores) / len(match_scores) if match_scores else 0.0

        logger.info(
            f"Semantic skill matching: {len(semantically_matched)} skills matched "
            f"(similarity: {semantic_score:.2f})"
        )

        return semantic_score, semantically_matched

    async def calculate_job_description_similarity(
        self, resume_summary: str, job_description_summary: str
    ) -> float:
        """Calculate semantic similarity between resume and job description.

        Returns:
            Similarity score (0.0 to 1.0)
        """
        if not resume_summary or not job_description_summary:
            return 0.0

        try:
            embeddings = await self.embedding_provider.embed_batch(
                [resume_summary, job_description_summary]
            )
        except Exception as e:
            logger.error(f"Embedding provider failed during JD similarity: {e}")
            raise EmbeddingUnavailableError(str(e)) from e

        similarity = self._cosine_similarity(embeddings[0], embeddings[1])
        logger.info(f"Job description semantic similarity: {similarity:.2f}")
        return similarity

    @staticmethod
    def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0

        dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot_product / (norm_a * norm_b)
