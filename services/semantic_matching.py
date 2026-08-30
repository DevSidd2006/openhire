"""
Semantic matching using embeddings for resume-to-job matching.
Provides semantic similarity scoring using embeddings from NVIDIA NIM.
"""
from typing import List, Tuple
import math

from core.logging import get_logger
from providers import get_embedding_provider

logger = get_logger("services.semantic_matching")


class SemanticMatcher:
    """Semantic matching using embeddings."""

    def __init__(self):
        self.embedding_provider = get_embedding_provider()

    async def calculate_skill_semantic_similarity(
        self, candidate_skills: List[str], required_skills: List[str], preferred_skills: List[str]
    ) -> Tuple[float, List[str]]:
        """
        Calculate semantic similarity between candidate skills and job requirements.

        Returns:
            (semantic_match_score, semantically_matched_skills)
        """
        if not required_skills or not candidate_skills:
            return 0.0, []

        try:
            # Generate embeddings for all skills
            all_skills = list(set(candidate_skills + required_skills + preferred_skills))
            skill_embeddings = await self.embedding_provider.embed_batch(all_skills)
            skill_to_embedding = dict(zip(all_skills, skill_embeddings))

            # Find semantic matches
            semantically_matched = []
            match_scores = []

            for req_skill in required_skills:
                req_embedding = skill_to_embedding[req_skill]
                best_similarity = 0.0

                for cand_skill in candidate_skills:
                    cand_embedding = skill_to_embedding[cand_skill]
                    similarity = self._cosine_similarity(req_embedding, cand_embedding)

                    if similarity > best_similarity:
                        best_similarity = similarity

                    # Consider it a match if similarity > 0.7
                    if similarity > 0.7 and cand_skill not in semantically_matched:
                        semantically_matched.append(cand_skill)

                match_scores.append(best_similarity)

            # Average similarity score for required skills
            semantic_score = sum(match_scores) / len(match_scores) if match_scores else 0.0

            logger.info(
                f"Semantic skill matching: {len(semantically_matched)} skills matched "
                f"(similarity: {semantic_score:.2f})"
            )

            return semantic_score, semantically_matched

        except Exception as e:
            logger.warning(f"Semantic skill matching failed, falling back to exact matching: {e}")
            return 0.0, []

    async def calculate_job_description_similarity(
        self, resume_summary: str, job_description_summary: str
    ) -> float:
        """
        Calculate semantic similarity between resume and job description.

        Returns:
            Similarity score (0.0 to 1.0)
        """
        if not resume_summary or not job_description_summary:
            return 0.0

        try:
            # Generate embeddings
            embeddings = await self.embedding_provider.embed_batch(
                [resume_summary, job_description_summary]
            )
            resume_embedding = embeddings[0]
            job_embedding = embeddings[1]

            # Calculate cosine similarity
            similarity = self._cosine_similarity(resume_embedding, job_embedding)

            logger.info(f"Job description semantic similarity: {similarity:.2f}")

            return similarity

        except Exception as e:
            logger.warning(f"Job description similarity calculation failed: {e}")
            return 0.0

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
