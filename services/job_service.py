"""
Job use cases.

Wraps the existing `JDAnalyzerAgent` (agents/jd_analyzer/agent.py) - a raw
job-description text goes in, a structured `JobDescription`
(schemas/job.py) comes out, exactly as it always has via
`orchestration/graph.py:node_analyze_job`. This service does not
reimplement or duplicate that extraction; it is the only thing that adds a
persistence boundary and an HTTP-shaped error contract around it.

No interview intelligence here either: `JobDescription.competencies` still
drives the adaptive interview and scoring exactly as before - this layer
only creates, stores, updates and archives that object. It never invents a
competency, a skill, or a weight.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from agents.jd_analyzer.agent import JDAnalyzerAgent
from agents.rubric_generator.agent import RubricGeneratorAgent
from core.errors import ConflictError, DependencyError, NotFoundError
from core.logging import get_logger, log_context
from repositories.interfaces import JobRecord, JobRepository, RubricRepository
from schemas.job import JobDescription

logger = get_logger("services.job")


class JobService:
    """Use cases for job postings.

    Constructed per request, like `InterviewService` - it holds only
    references (a repository, an optional agent factory), never state.
    """

    def __init__(
        self,
        *,
        job_repository: JobRepository,
        jd_analyzer_factory=None,
        rubric_repository: Optional[RubricRepository] = None,
        rubric_generator_factory=None,
    ) -> None:
        self._jobs = job_repository
        # The same wiring-seam pattern as InterviewService's
        # `interviewer_factory`: None means "let JDAnalyzerAgent resolve its
        # own default LLM provider", which is what a real deployment does.
        self._jd_analyzer_factory = jd_analyzer_factory
        # `rubric_repository` is optional so existing/other-test callers that
        # only care about job CRUD (most of this file) are unaffected - only
        # core/dependencies.py's real wiring supplies one, which is what
        # makes auto-rubric-on-create active in the actual app.
        self._rubrics = rubric_repository
        self._rubric_generator_factory = rubric_generator_factory

    async def create_job(
        self, *, description: str, job_id: Optional[str] = None, openings: Optional[int] = None
    ) -> JobRecord:
        """Analyze raw job-description text into a structured `JobDescription`
        and store it.

        `job_id` is generated here (uuid4, matching the pattern
        `api.registry.SessionRegistry` already uses for session_id) when the
        caller doesn't supply one, rather than being invented by the LLM -
        JDAnalyzerAgent takes it as an input, not an output.
        """
        job_id = job_id or f"job_{uuid.uuid4().hex[:8]}"
        analyzer = self._jd_analyzer_factory() if self._jd_analyzer_factory else JDAnalyzerAgent()

        agent_result = await analyzer.run(
            run_id=f"job_create_{job_id}",
            job_description=description,
            job_id=job_id,
        )
        result = (agent_result or {}).get("result") or {}
        job_description = result.get("job_description")

        if job_description is None:
            # JDAnalyzerAgent never fabricates a JobDescription on failure
            # (see its own module docstring) - a missing result here is a
            # genuine upstream failure, not something this layer can repair.
            error_reason = result.get("error") or (agent_result or {}).get("error")
            logger.error(
                "job analysis failed: %s",
                error_reason,
                extra=log_context(event="job_analysis_failed", job_id=job_id),
            )
            raise DependencyError(
                "The job description could not be analyzed. Please try again.",
                internal_detail=f"JDAnalyzerAgent failed for job_id={job_id!r}: {error_reason}",
                context={"job_id": job_id},
            )

        if openings is not None and openings >= 1:
            job_description = job_description.model_copy(update={"openings": openings})

        # Rubric BEFORE the job is persisted: this product's flow has no
        # recruiter step between posting a job and the leaderboard
        # (shortlisting is rubric-decided), so a job must never exist
        # without an approved rubric - if rubric generation fails, no
        # JobRecord should be saved at all, exactly like a failed
        # JDAnalyzerAgent call above never produces one either.
        if self._rubrics is not None:
            await self._draft_and_approve_rubric(job_id=job_id, job_description=job_description)

        record = JobRecord(job_id=job_id, job=job_description, is_active=True)
        stored = await self._jobs.save(record)
        logger.info(
            "job created",
            extra=log_context(event="job_created", job_id=job_id,
                              competency_count=len(job_description.competencies)),
        )
        return stored

    async def _draft_and_approve_rubric(
        self, *, job_id: str, job_description: JobDescription
    ) -> None:
        """Make the job scorable the instant it is created - this product's
        flow has no recruiter step between posting a job and the
        leaderboard (shortlisting is rubric-decided), so unlike the old
        manual "draft, then separately approve" flow on job-detail.html, a
        job must never exist without an approved rubric.

        Reuses the exact same generation + approval calls
        `POST /jobs/{id}/rubric/draft` and `/approve` already make
        (api/routes/rubrics.py) - RubricRepository.approve() itself enforces
        `JobRubric.validate_approvable()`, so a malformed rubric still
        cannot become active.

        Raises DependencyError on any failure - a job is never left
        half-created (JD analyzed, no rubric), mirroring how a failed
        JDAnalyzerAgent call above already aborts job creation entirely.
        """
        generator = self._rubric_generator_factory() if self._rubric_generator_factory else RubricGeneratorAgent()
        try:
            result = await generator.execute(job_description=job_description)
            drafted = result["rubric"]
            saved = await self._rubrics.save(drafted.model_copy(update={"job_id": job_id, "version": 1}))
            await self._rubrics.approve(saved.rubric_id)
        except Exception as exc:
            logger.error(
                "rubric auto-generation failed: %s",
                exc,
                extra=log_context(event="rubric_auto_generation_failed", job_id=job_id),
            )
            raise DependencyError(
                "The job's scoring rubric could not be generated. Please try again.",
                internal_detail=f"rubric auto-draft/approve failed for job_id={job_id!r}: {exc}",
                context={"job_id": job_id},
            ) from exc

    async def get_job(self, job_id: str) -> JobRecord:
        record = await self._jobs.get(job_id)
        if record is None:
            raise NotFoundError(
                "No job found for the given job_id",
                internal_detail=f"job record missing for job_id={job_id!r}",
            )
        return record

    async def list_jobs(self, *, include_archived: bool = False) -> list[JobRecord]:
        return await self._jobs.list_jobs(include_archived=include_archived)

    async def update_job(self, job_id: str, patch: Dict[str, Any]) -> JobRecord:
        """Apply a partial field update to a stored job.

        Deliberately does NOT re-invoke `JDAnalyzerAgent`: mixing "the
        recruiter hand-edited these three fields" with "the LLM re-analyzed
        the whole description" in one operation makes it ambiguous which one
        wins, and a raw-text re-analysis is already available by archiving
        and re-creating the job. `patch` only ever contains fields the
        caller explicitly set (see api/models_jobs.py's
        `UpdateJobRequest.model_dump(exclude_unset=True)`), so an omitted
        field is left exactly as stored, never reset to a default.

        Reconstructing via `JobDescription(**merged)` (rather than
        `model_copy(update=patch)`) is deliberate: it re-runs
        `JobDescription`'s own validators (competency weights summing to
        1.0), so an edit that breaks that invariant is rejected with the
        same 422 a bad `POST /jobs` payload would get, not silently stored.
        """
        record = await self.get_job(job_id)
        if not record.is_active:
            raise ConflictError(
                "Cannot update an archived job",
                internal_detail=f"job_id={job_id!r} is archived",
            )

        merged = record.job.model_dump()
        merged.update(patch)
        updated_job = JobDescription(**merged)

        updated_record = record.model_copy(update={"job": updated_job})
        stored = await self._jobs.save(updated_record)
        logger.info("job updated", extra=log_context(event="job_updated", job_id=job_id))
        return stored

    async def archive_job(self, job_id: str) -> JobRecord:
        """Soft-delete: mark a job as no longer accepting applications.

        No hard delete - `Application.job_id` and `SessionRecord.job_id`
        both reference a job by id, and destroying the record would orphan
        both. Idempotent: archiving an already-archived job just returns its
        current state rather than erroring.
        """
        record = await self._jobs.archive(job_id)
        if record is None:
            raise NotFoundError(
                "No job found for the given job_id",
                internal_detail=f"job record missing for job_id={job_id!r}",
            )
        logger.info("job archived", extra=log_context(event="job_archived", job_id=job_id))
        return record


__all__ = ["JobService"]
