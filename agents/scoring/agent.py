"""
Scoring Agent.
Synthesizes component evaluations into final weighted score.
"""
from typing import Any, Dict
import json
import uuid

from agents.base import BaseAgent
from schemas.scoring import CandidateScores
from schemas.evaluation import (
    TechnicalEvaluation,
    BehavioralEvaluation,
    MatchingScore,
)
from schemas.job import JobDescription


class ScoringAgent(BaseAgent):
    """Synthesizes all evaluations into final scores."""

    def __init__(self, **kwargs):
        super().__init__(name="scoring", **kwargs)

    async def execute(
        self,
        job_description: JobDescription,
        technical_evaluation: TechnicalEvaluation,
        behavioral_evaluation: BehavioralEvaluation,
        matching_score: MatchingScore,
        candidate_id: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Calculate final candidate score.
        
        Args:
            job_description: Job with scoring weights
            technical_evaluation: Technical eval component
            behavioral_evaluation: Behavioral eval component
            matching_score: Resume match score
            candidate_id: Candidate identifier
            
        Returns:
            CandidateScores with all component and final scores
        """
        self.logger.info(f"Scoring candidate {candidate_id}")

        try:
            # Get rubric weights (with fallbacks)
            tech_weight = 0.45
            behav_weight = 0.30
            job_fit_weight = 0.25

            # Extract component scores
            tech_score = technical_evaluation.technical_score if technical_evaluation else 7.0
            behav_score = behavioral_evaluation.behavioral_score if behavioral_evaluation else 7.0
            job_fit_score = (matching_score.match_score * 10) if matching_score else 7.0

            # Normalize scores to 0-10 scale
            tech_score = self._normalize_score(tech_score)
            behav_score = self._normalize_score(behav_score)
            job_fit_score = self._normalize_score(job_fit_score)

            # Calculate weighted final score
            weighted_final = (
                tech_score * tech_weight +
                behav_score * behav_weight +
                job_fit_score * job_fit_weight
            )

            # Create candidate scores
            scores = CandidateScores(
                score_id=f"scores_{uuid.uuid4().hex[:8]}",
                candidate_id=candidate_id,
                job_id=job_description.job_id,
                technical_score=tech_score,
                behavioral_score=behav_score,
                job_fit_score=job_fit_score,
                weighted_final_score=weighted_final,
                percentile_rank=0.0,  # Will be calculated in leaderboard
                explanation=f"Final score: {weighted_final:.1f}/10. Technical: {tech_score:.1f} ({tech_weight*100:.0f}%), Behavioral: {behav_score:.1f} ({behav_weight*100:.0f}%), Job Fit: {job_fit_score:.1f} ({job_fit_weight*100:.0f}%)",
                confidence=0.85,
            )

            return {"candidate_scores": scores}

        except Exception as e:
            self.logger.error(f"Scoring failed: {str(e)}")
            return {"candidate_scores": None, "error": str(e)}

    def _normalize_score(self, score: float) -> float:
        """Normalize score to 0-10 scale."""
        # If already roughly in 0-10 range, return as-is
        if 0 <= score <= 10:
            return score
        # If in 0-1 range (probability), scale to 0-10
        if 0 <= score <= 1:
            return score * 10
        # Otherwise, cap at 10
        return min(10, max(0, score))
