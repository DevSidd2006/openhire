"""
Resume Matcher Agent.
Compares resumes against job descriptions to calculate match scores.
"""
from typing import Any, Dict
import json
import uuid

from agents.base import BaseAgent
from schemas.evaluation import MatchingScore
from schemas.job import JobDescription
from schemas.resume import ParsedResume


class ResumeMatcherAgent(BaseAgent):
    """Matches candidate resumes to job requirements."""

    def __init__(self, **kwargs):
        super().__init__(name="resume_matcher", **kwargs)

    async def execute(
        self,
        job_description: JobDescription,
        parsed_resume: ParsedResume,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Match resume to job description.
        
        Args:
            job_description: Parsed job description
            parsed_resume: Parsed resume
            
        Returns:
            MatchingScore with match analysis
        """
        self.logger.info(
            f"Matching candidate {parsed_resume.candidate_id} to job {job_description.job_id}"
        )

        try:
            # Calculate match scores
            match_score = await self._calculate_match(job_description, parsed_resume)

            return {
                "matching_score": match_score,
                "shortlist_recommendation": match_score.shortlist_recommendation,
                "match_percentage": match_score.match_score * 100,
            }

        except Exception as e:
            self.logger.error(f"Matching failed: {str(e)}")
            return {"matching_score": None, "error": str(e)}

    async def _calculate_match(
        self, job_description: JobDescription, parsed_resume: ParsedResume
    ) -> MatchingScore:
        """Calculate match score between resume and job."""

        # Compare skills
        candidate_skills_lower = [s.lower() for s in parsed_resume.skills]
        required_skills_lower = [s.lower() for s in job_description.required_skills]
        preferred_skills_lower = [s.lower() for s in job_description.preferred_skills]

        # Find matches
        skill_matches = [
            s for s in job_description.required_skills
            if s.lower() in candidate_skills_lower
        ]
        missing_required = [
            s for s in job_description.required_skills
            if s.lower() not in candidate_skills_lower
        ]
        missing_preferred = [
            s for s in job_description.preferred_skills
            if s.lower() not in candidate_skills_lower
        ]

        # Calculate scores
        required_skill_match = len(skill_matches) / len(required_skills_lower) if required_skills_lower else 1.0
        preferred_skill_match = (
            (len(job_description.preferred_skills) - len(missing_preferred)) / len(preferred_skills_lower)
            if preferred_skills_lower
            else 0.5
        )

        # Experience match
        experience_match = self._calculate_experience_match(
            parsed_resume.total_experience_years,
            job_description.experience_years
        )

        # Overall match score (weighted)
        match_score = (required_skill_match * 0.5 + preferred_skill_match * 0.2 + experience_match * 0.3)

        # Shortlist recommendation
        shortlist = match_score >= 0.6 and len(missing_required) <= 2

        return MatchingScore(
            match_id=f"match_{uuid.uuid4().hex[:8]}",
            candidate_id=parsed_resume.candidate_id,
            job_id=job_description.job_id,
            match_score=match_score,
            skill_matches=skill_matches,
            missing_required_skills=missing_required,
            missing_preferred_skills=missing_preferred,
            experience_match=experience_match,
            skill_gap=1.0 - required_skill_match,
            evidence=[],  # Could add detailed evidence
            explanation=f"Candidate matches {len(skill_matches)}/{len(required_skills_lower)} required skills",
            shortlist_recommendation=shortlist,
            confidence=0.85,
        )

    def _calculate_experience_match(
        self, candidate_years: float, required_years: int
    ) -> float:
        """Calculate experience match score."""
        if candidate_years is None or required_years is None:
            return 0.5  # Neutral if unknown

        if candidate_years >= required_years:
            # More experience is good, but diminishing returns
            excess = min(candidate_years - required_years, required_years)
            return min(1.0, (required_years + excess * 0.25) / required_years)
        else:
            # Less experience is negative but not disqualifying
            return candidate_years / required_years
