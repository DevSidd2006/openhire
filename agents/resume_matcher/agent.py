"""
Resume Matcher Agent.
Compares resumes against job descriptions to calculate match scores.
Uses both exact string matching and semantic similarity via embeddings.
"""
from typing import Any, Dict
import json
import uuid

from agents.base import BaseAgent
from schemas.evaluation import MatchingScore
from schemas.job import JobDescription
from schemas.resume import ParsedResume
from services.semantic_matching import SemanticMatcher


class ResumeMatcherAgent(BaseAgent):
    """Matches candidate resumes to job requirements using exact and semantic matching."""

    def __init__(self, semantic_matcher=None, **kwargs):
        super().__init__(name="resume_matcher", **kwargs)
        self.semantic_matcher = semantic_matcher or SemanticMatcher()

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
        """Calculate match score between resume and job using exact and semantic matching."""

        # Exact skill matching
        candidate_skills_lower = [s.lower() for s in parsed_resume.skills]
        required_skills_lower = [s.lower() for s in job_description.required_skills]
        preferred_skills_lower = [s.lower() for s in job_description.preferred_skills]

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

        # Calculate exact match score
        exact_skill_match = len(skill_matches) / len(required_skills_lower) if required_skills_lower else 1.0
        preferred_skill_match = (
            (len(job_description.preferred_skills) - len(missing_preferred)) / len(preferred_skills_lower)
            if preferred_skills_lower
            else 0.5
        )

        # Semantic matching using embeddings
        semantic_skill_score, semantic_matches = await self.semantic_matcher.calculate_skill_semantic_similarity(
            parsed_resume.skills,
            job_description.required_skills,
            job_description.preferred_skills
        )

        # Combine exact and semantic skill matching (25% exact, 75% semantic)
        # Semantic matching via embeddings is more reliable for skill matching since
        # job descriptions and resumes use different terminology for identical concepts
        # (e.g., "CI/CD (GitHub Actions)" vs "CI/CD", "LLM Integration" vs "Machine Learning/LLM Integration")
        combined_skill_match = (exact_skill_match * 0.25) + (semantic_skill_score * 0.75)

        # Job description semantic similarity
        resume_summary = f"{parsed_resume.summary or ''} {' '.join(parsed_resume.skills)}"
        job_summary = f"{job_description.description or ''} {' '.join(job_description.required_skills)}"
        jd_similarity = await self.semantic_matcher.calculate_job_description_similarity(
            resume_summary, job_summary
        )

        # Experience match
        experience_match = self._calculate_experience_match(
            parsed_resume.total_experience_years,
            job_description.experience_years
        )

        # Overall match score (weighted combination)
        # Skills: 50%, Job description fit: 20%, Experience: 30%
        match_score = (
            combined_skill_match * 0.5 +
            jd_similarity * 0.2 +
            experience_match * 0.3
        )

        # Shortlist recommendation
        # Require good skill match and job description fit
        shortlist = match_score >= 0.6 and combined_skill_match >= 0.5

        explanation = (
            f"Candidate matches {len(skill_matches)}/{len(required_skills_lower)} required skills (exact) "
            f"+ {len(semantic_matches)} semantic matches. "
            f"Job fit score: {jd_similarity:.2f}"
        )

        return MatchingScore(
            match_id=f"match_{uuid.uuid4().hex[:8]}",
            candidate_id=parsed_resume.candidate_id,
            job_id=job_description.job_id,
            match_score=match_score,
            skill_matches=skill_matches,
            missing_required_skills=missing_required,
            missing_preferred_skills=missing_preferred,
            experience_match=experience_match,
            skill_gap=1.0 - combined_skill_match,
            evidence=[],
            explanation=explanation,
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
