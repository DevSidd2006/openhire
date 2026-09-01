"""
Matching use case.

A thin wrapper around the existing `ResumeMatcherAgent`
(agents/resume_matcher/agent.py) - the same deterministic skill/experience
comparison `orchestration/graph.py:node_match_resumes` already uses to
populate `PipelineState.matching_scores` and `shortlisted_candidates`. No
matching algorithm is reimplemented here; this only adds the HTTP-shaped
error contract `ApplicationService` needs to turn "matching failed" into a
clean, typed error instead of a raw exception.
"""
from __future__ import annotations

from typing import Optional

from agents.resume_matcher.agent import ResumeMatcherAgent
from core.errors import DependencyError
from core.logging import get_logger, log_context
from schemas.evaluation import MatchingScore
from schemas.rubric import JobRubric
from schemas.job import JobDescription
from schemas.resume import ParsedResume

logger = get_logger("services.matching")


class MatchingService:
    """Use case for resume-to-job matching."""

    def __init__(self, *, resume_matcher_factory=None) -> None:
        self._resume_matcher_factory = resume_matcher_factory

    async def compute_match(
        self, job_rubric: JobRubric, parsed_resume: ParsedResume
    ) -> MatchingScore:
        """Match one candidate's resume against one job.

        Reuses `ResumeMatcherAgent.execute()` exactly as the batch pipeline
        does; the only difference is that a caller here gets a typed
        `DependencyError` instead of an unhandled exception or a bare
        `{"matching_score": None, "error": ...}` dict on failure.
        """
        matcher = self._resume_matcher_factory() if self._resume_matcher_factory else ResumeMatcherAgent()

        agent_result = await matcher.run(
            run_id=f"match_{job_rubric.job_id}_{parsed_resume.candidate_id}",
            job_rubric=job_rubric,
            parsed_resume=parsed_resume,
        )
        result = (agent_result or {}).get("result") or {}
        matching_score: Optional[MatchingScore] = result.get("matching_score")

        if matching_score is None:
            error_reason = result.get("error") or (agent_result or {}).get("error")
            logger.error(
                "matching failed: %s",
                error_reason,
                extra=log_context(
                    event="matching_failed",
                    job_id=job_rubric.job_id,
                    candidate_id=parsed_resume.candidate_id,
                ),
            )
            raise DependencyError(
                "Matching could not be computed for this candidate. Please try again.",
                internal_detail=(
                    f"ResumeMatcherAgent failed for job_id={job_rubric.job_id!r} "
                    f"candidate_id={parsed_resume.candidate_id!r}: {error_reason}"
                ),
                context={"job_id": job_rubric.job_id, "candidate_id": parsed_resume.candidate_id},
            )

        return matching_score


__all__ = ["MatchingService"]
