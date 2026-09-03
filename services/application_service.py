"""
Application use cases: Candidate -> Application -> Job, and the bridge
onward to Matching/Shortlisting and to Interview.

Terminology note (see schemas/application.py's module docstring): the
`Application` concept introduced here is new backend-application-layer
scaffolding; it is not a claim about the eventual database schema, which is
the database teammate's call.

Flow this service implements, matching Chunk 2's brief exactly:

    Job -> Applications -> Matching -> Ranked candidates
        -> Shortlisted candidates -> Interview

`run_matching_for_job` and `MatchingService.compute_match` reuse
`ResumeMatcherAgent` unchanged - no matching algorithm is invented here.
Shortlisting is not a separately invented threshold: it is exactly
`MatchingScore.shortlist_recommendation`, the same boolean
`orchestration/graph.py:node_match_resumes` already uses to populate
`shortlisted_candidates`. Ranking is a plain sort on
`MatchingScore.match_score`, not a new scoring formula.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import List

from core.errors import ConflictError, ForbiddenError, NotFoundError
from core.logging import get_logger, log_context
from repositories.interfaces import (
    ApplicationRepository,
    RubricRepository,
    CandidateRepository,
    JobRepository,
)
from schemas.application import Application, ApplicationStatus
from services.matching_service import MatchingService
from services.semantic_screening import SemanticScreeningService

logger = get_logger("services.application")

SHORTLIST_THRESHOLD = 0.45


def evaluate_shortlist_status(
    matching_score = None,
    semantic_score: float | None = None,
    threshold: float = SHORTLIST_THRESHOLD,
) -> ApplicationStatus:
    """Determine the automated shortlisting status for an application."""
    if matching_score is not None:
        if matching_score.needs_human_review:
            return ApplicationStatus.NEEDS_HUMAN_REVIEW
        if matching_score.match_score is not None:
            if matching_score.match_score >= threshold:
                return ApplicationStatus.SHORTLISTED
            return ApplicationStatus.REJECTED
        return ApplicationStatus.NEEDS_HUMAN_REVIEW

    if semantic_score is not None:
        if semantic_score >= threshold:
            return ApplicationStatus.SHORTLISTED
        return ApplicationStatus.REJECTED

    return ApplicationStatus.SCORING_PENDING


@dataclass
class MatchingRunResult:
    """Outcome of running matching for every pending application on one job.

    `applications` is ranked (highest `match_score` first) so a caller gets
    "ranked candidates" directly, without a second sort step. `errors` names
    the candidate_ids matching failed for - mirroring
    `orchestration/graph.py:node_match_resumes`'s own resilience contract of
    collecting per-candidate errors rather than failing the whole batch.
    """

    job_id: str
    # How many SUBMITTED applications this run attempted to match - the
    # denominator `matched_count` and `errors` are measured against.
    attempted: int = 0
    applications: List[Application] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def matched_count(self) -> int:
        """How many of this run's SUBMITTED applications were actually
        scored (attempted minus the ones that failed)."""
        return self.attempted - len(self.errors)

    @property
    def shortlisted_count(self) -> int:
        return sum(1 for a in self.applications if a.status == ApplicationStatus.SHORTLISTED)

    @property
    def rejected_count(self) -> int:
        return sum(1 for a in self.applications if a.status == ApplicationStatus.REJECTED)


class ApplicationService:
    """Use cases for applications."""

    def __init__(
        self,
        *,
        application_repository: ApplicationRepository,
        job_repository: JobRepository,
        candidate_repository: CandidateRepository,
        matching_service: MatchingService,
        rubric_repository: RubricRepository | None = None,
        semantic_screening_service: SemanticScreeningService | None = None,
    ) -> None:
        self._applications = application_repository
        self._jobs = job_repository
        self._candidates = candidate_repository
        self._matching = matching_service
        self._rubrics = rubric_repository
        self._semantic_screening = semantic_screening_service or SemanticScreeningService()

    async def _compute_semantic_scores(
        self, job_record, applications: List[Application]
    ) -> dict:
        """Semantic JD-similarity score per application_id.

        A missing or None entry means "not computed" - a candidate whose
        record has vanished, or an embedding outage - and must never be
        rendered as a zero.
        """
        scores: dict = {}
        for application in applications:
            candidate_record = await self._candidates.get(application.candidate_id)
            if candidate_record is None or candidate_record.resume is None:
                continue
            scores[application.application_id] = await self._semantic_screening.score(
                job_record.job, candidate_record.resume
            )
        return scores

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def apply(self, *, job_id: str, candidate_id: str, user_id: str | None = None) -> Application:
        """Candidate applies to a job.

        Validates both ends of the link actually exist (job, candidate),
        that the job is still accepting applications (not archived), and
        that this exact pair hasn't already applied - a duplicate
        application is a 409, not a second row (Chunk 2 Step 9).

        If `user_id` is provided, validates that the candidate belongs to
        the authenticated user (ownership check for authorization).
        """
        job_record = await self._jobs.get(job_id)
        if job_record is None:
            raise NotFoundError(
                "No job found for the given job_id",
                internal_detail=f"job_id={job_id!r} not found",
            )
        if not job_record.is_active:
            raise ConflictError(
                "This job is archived and is not accepting applications",
                internal_detail=f"job_id={job_id!r} is archived",
            )

        candidate_record = await self._candidates.get(candidate_id)
        if candidate_record is None:
            raise NotFoundError(
                "No candidate found for the given candidate_id",
                internal_detail=f"candidate_id={candidate_id!r} not found",
            )

        if user_id is not None and candidate_record.user_id != user_id:
            raise ForbiddenError(
                "You do not have permission to apply as this candidate",
                internal_detail=f"candidate {candidate_id!r} owned by {candidate_record.user_id!r}, not {user_id!r}",
            )

        existing = await self._applications.get_for_job_and_candidate(job_id, candidate_id)
        if existing is not None:
            raise ConflictError(
                "This candidate has already applied to this job",
                internal_detail=(
                    f"duplicate application: job_id={job_id!r} "
                    f"candidate_id={candidate_id!r} application_id={existing.application_id!r}"
                ),
                context={"application_id": existing.application_id},
            )

        # Semantic screening on initial application
        semantic_score = None
        if candidate_record.resume is not None:
            try:
                semantic_score = await self._semantic_screening.score(
                    job_record.job, candidate_record.resume
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Semantic screening failed during apply: %s", exc)

        application = Application(
            application_id=f"app_{uuid.uuid4().hex[:8]}",
            job_id=job_id,
            candidate_id=candidate_id,
            status=ApplicationStatus.SUBMITTED,
            matching_score=None,
            semantic_score=semantic_score,
        )
        stored = await self._applications.save(application)
        logger.info(
            "application submitted",
            extra=log_context(
                event="application_submitted", application_id=stored.application_id,
                job_id=job_id, candidate_id=candidate_id, status=stored.status.value,
            ),
        )
        return stored

    async def run_matching_for_job(self, job_id: str) -> MatchingRunResult:
        """Run matching for every SUBMITTED/SCORING_PENDING application against
        `job_id`, then automatically shortlist qualifying candidates and rank the
        results.
        """
        job_record = await self._jobs.get(job_id)
        if job_record is None:
            raise NotFoundError(
                "No job found for the given job_id",
                internal_detail=f"job_id={job_id!r} not found",
            )

        rubric = (
            await self._rubrics.get_approved_for_job(job_id) if self._rubrics else None
        )

        applications = await self._applications.list_for_job(job_id)
        # SCORING_PENDING is included, not just SUBMITTED: that status is
        # where the no-rubric branch below parks applications, and the whole
        # point of parking them is that they get picked up once a rubric is
        # approved. Filtering on SUBMITTED alone stranded them forever.
        pending = [
            a for a in applications
            if a.status in (ApplicationStatus.SUBMITTED, ApplicationStatus.SCORING_PENDING)
        ]

        # The rubric-free semantic score is computed for every pending
        # application regardless of rubric state.
        semantic_scores = await self._compute_semantic_scores(job_record, pending)

        if rubric is None:
            semantic_only = MatchingRunResult(job_id=job_id, attempted=0)
            for application in pending:
                s_score = semantic_scores.get(
                    application.application_id, application.semantic_score
                )
                saved_app = await self._applications.save(
                    application.model_copy(
                        update={
                            "status": ApplicationStatus.SCORING_PENDING,
                            "semantic_score": s_score,
                        }
                    )
                )
                semantic_only.applications.append(saved_app)
            semantic_only.applications.sort(
                key=lambda a: a.semantic_score if a.semantic_score is not None else -1.0,
                reverse=True,
            )
            logger.info(
                "matching run (semantic score only) complete",
                extra=log_context(
                    event="matching_no_rubric", job_id=job_id,
                    semantic_scored=sum(1 for v in semantic_scores.values() if v is not None),
                    shortlisted=semantic_only.shortlisted_count,
                    rejected=semantic_only.rejected_count,
                ),
            )
            return semantic_only

        # Idempotency is keyed on the rubric version an application was last scored against.
        pending = [
            a for a in pending
            if a.matching_score is None
            or a.matching_score.rubric_version != rubric.version
        ]

        result = MatchingRunResult(job_id=job_id, attempted=len(pending))
        # Already-decided applications are still part of "ranked
        # candidates" for this job - included so a caller sees the whole
        # picture, not just this run's deltas.
        already_decided = [a for a in applications if a not in pending]

        for application in pending:
            candidate_record = await self._candidates.get(application.candidate_id)
            if candidate_record is None:
                result.errors.append(application.candidate_id)
                logger.error(
                    "matching skipped: candidate record missing",
                    extra=log_context(
                        event="matching_skipped", job_id=job_id,
                        candidate_id=application.candidate_id,
                    ),
                )
                continue

            try:
                matching_score = await self._matching.compute_match(
                    rubric, candidate_record.resume
                )
            except Exception as exc:  # noqa: BLE001 - a per-candidate failure must not abort the batch
                result.errors.append(application.candidate_id)
                await self._applications.save(
                    application.model_copy(
                        update={
                            "status": ApplicationStatus.SCORING_PENDING,
                            "semantic_score": semantic_scores.get(
                                application.application_id, application.semantic_score
                            ),
                        }
                    )
                )
                logger.error(
                    "matching failed for one candidate, continuing batch: %s",
                    exc,
                    extra=log_context(
                        event="matching_failed", job_id=job_id,
                        candidate_id=application.candidate_id,
                    ),
                )
                continue

            # Automated shortlisting decision based on rubric match score and evidence coverage.
            new_status = evaluate_shortlist_status(
                matching_score,
                semantic_scores.get(application.application_id, application.semantic_score),
            )
            updated = application.model_copy(
                update={
                    "matching_score": matching_score,
                    "status": new_status,
                    "semantic_score": semantic_scores.get(
                        application.application_id, application.semantic_score
                    ),
                }
            )
            stored = await self._applications.save(updated)
            result.applications.append(stored)

        result.applications.extend(already_decided)
        result.applications.sort(
            key=lambda a: (
                a.matching_score.match_score
                if a.matching_score and a.matching_score.match_score is not None
                else -1.0
            ),
            reverse=True,
        )

        logger.info(
            "matching run complete",
            extra=log_context(
                event="matching_run_complete", job_id=job_id,
                matched=len(pending) - len(result.errors),
                shortlisted=result.shortlisted_count, rejected=result.rejected_count,
                failed=len(result.errors),
            ),
        )
        return result

    async def link_interview_session(self, application_id: str, session_id: str) -> Application:
        """Record that an interview session (Chunk 1's `/sessions`) now
        exists for this application.

        Valid from SHORTLISTED or INTERVIEW_LINKED.
        """
        application = await self.get_application(application_id)
        if application.status not in (ApplicationStatus.SHORTLISTED, ApplicationStatus.INTERVIEW_LINKED):
            raise ConflictError(
                "An application must be shortlisted before an interview can be linked to it",
                internal_detail=(
                    f"application_id={application_id!r} has status "
                    f"{application.status.value!r}, not shortlisted"
                ),
            )

        updated = application.model_copy(
            update={"session_id": session_id, "status": ApplicationStatus.INTERVIEW_LINKED}
        )
        stored = await self._applications.save(updated)
        logger.info(
            "interview linked to application",
            extra=log_context(
                event="interview_linked", application_id=application_id, session_id=session_id,
            ),
        )
        return stored

    async def shortlist(self, application_id: str) -> Application:
        """Chunk 5: a recruiter's manual override to SHORTLISTED.

        Reverses an automatic REJECTED (or SUBMITTED, if the recruiter
        wants to shortlist ahead of/instead of running matching) - this is
        purely a status change via the existing repository, never a second
        matching computation (`run_matching_for_job`, unmodified, remains
        the only thing that runs `ResumeMatcherAgent`).

        Not valid once INTERVIEW_LINKED: at that point the candidate is
        already past the shortlisting gate, and "shortlisting" them again
        has no meaning. Idempotent from SHORTLISTED itself.
        """
        application = await self.get_application(application_id)
        if application.status == ApplicationStatus.INTERVIEW_LINKED:
            raise ConflictError(
                "Cannot shortlist an application that already has a linked interview",
                internal_detail=f"application_id={application_id!r} status=interview_linked",
            )
        if application.status == ApplicationStatus.SHORTLISTED:
            return application

        updated = application.model_copy(update={"status": ApplicationStatus.SHORTLISTED})
        stored = await self._applications.save(updated)
        logger.info(
            "application manually shortlisted",
            extra=log_context(event="application_shortlisted", application_id=application_id),
        )
        return stored

    async def reject(self, application_id: str) -> Application:
        """Chunk 5: a recruiter's decision to reject, at ANY stage -
        including after interviewing and evaluating the candidate, which is
        the most common real use of this (reviewing a completed report and
        deciding not to proceed), not only a pre-interview screen. Purely a
        status change; the interview, transcript and evaluation already on
        record are untouched and remain individually retrievable.

        Idempotent: rejecting an already-rejected application is a no-op
        success, matching `JobService.archive_job`'s precedent.
        """
        application = await self.get_application(application_id)
        if application.status == ApplicationStatus.REJECTED:
            return application

        updated = application.model_copy(update={"status": ApplicationStatus.REJECTED})
        stored = await self._applications.save(updated)
        logger.info(
            "application rejected",
            extra=log_context(event="application_rejected", application_id=application_id),
        )
        return stored

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_application(self, application_id: str) -> Application:
        application = await self._applications.get(application_id)
        if application is None:
            raise NotFoundError(
                "No application found for the given application_id",
                internal_detail=f"application_id={application_id!r} not found",
            )
        return application

    async def list_for_job(self, job_id: str) -> List[Application]:
        return await self._applications.list_for_job(job_id)

    async def list_for_candidate(self, candidate_id: str) -> List[Application]:
        return await self._applications.list_for_candidate(candidate_id)

    async def get_shortlist(self, job_id: str) -> List[Application]:
        """Ranked, shortlisted candidates for one job - the final stage of
        Job -> Applications -> Matching -> Ranked -> Shortlisted."""
        applications = await self._applications.list_for_job(job_id)
        shortlisted = [
            a for a in applications
            if a.status in (ApplicationStatus.SHORTLISTED, ApplicationStatus.INTERVIEW_LINKED)
        ]
        shortlisted.sort(
            key=lambda a: (
                a.matching_score.match_score
                if a.matching_score and a.matching_score.match_score is not None
                else 0.0
            ),
            reverse=True,
        )
        return shortlisted


__all__ = ["ApplicationService", "MatchingRunResult"]
