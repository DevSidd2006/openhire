"""
Recruiter-facing aggregation use cases (Chunk 5).

This service does not own any resource of its own - it assembles VIEWS
across four existing repositories (Job, Application, Candidate, Session)
plus the Evaluation repository, and produces the EXISTING ranking artifact
via the EXISTING `LeaderboardAgent`. Nothing here computes a score, decides
a recommendation, or re-implements matching/scoring/ranking logic that
already exists in Chunks 2/4.

Why this is its own service rather than living inside
JobService/ApplicationService
-----------------------------------------------------
`JobService` owns job postings; `ApplicationService` owns one application's
lifecycle. "Assemble a recruiter's view across a job's applications,
their candidates, their interviews and their evaluations" is a genuinely
different, cross-cutting concern that touches all of them read-only - giving
it its own service keeps each resource-owning service from accumulating
aggregation logic it doesn't otherwise need, exactly the same reasoning
that gave Chunk 4 its own `EvaluationService` rather than bolting evaluation
onto `InterviewService`.

Batch reads, not N+1 (Step 16)
--------------------------------
`list_job_applications` and `get_leaderboard` each make ONE call per
repository (via `CandidateRepository.get_many` / `SessionRepository.get_many`
/ `EvaluationRepository.get_many_for_sessions` - all added in this chunk)
regardless of how many applications a job has, rather than looping and
calling `.get()` once per application per repository.

Leaderboard consistency (Step 17)
------------------------------------
`get_leaderboard` never computes a score. It collects the ALREADY-STORED
`CandidateReport` for every application whose evaluation is COMPLETED
(`EvaluationJob.result` - the exact object `GET /evaluations/{id}` already
returns) and hands that list, unmodified, to the EXISTING `LeaderboardAgent`
- the same agent `orchestration/graph.py:node_create_leaderboard` calls,
called directly for the reason documented in
services/evaluation_service.py's module docstring (that pipeline is a
batch, from-raw-text entry point that does not fit calling it per job on
demand). A candidate's `weighted_score` on the leaderboard is therefore
always the exact `report.scores.weighted_final_score` already visible via
`GET /evaluations/{evaluation_id}` - never a second, independently
computed number.

Tie-breaking (Step 9)
------------------------
`LeaderboardAgent.execute` sorts by `weighted_final_score` alone
(`sorted(reports, key=..., reverse=True)`), which is a STABLE sort - ties
retain whatever order `reports` was in when handed to it. Left unspecified,
that would be repository iteration order, which Step 9 explicitly forbids
relying on. This service sorts `reports` by `candidate_id` BEFORE calling
the agent, so the stable sort turns that into an explicit, deterministic
secondary key ("highest score first, ties broken by candidate_id
ascending") without touching the agent's own ranking code at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from agents.leaderboard.agent import LeaderboardAgent
from core.errors import DependencyError, NotFoundError
from core.logging import get_logger, log_context
from repositories.interfaces import (
    Application,
    ApplicationRepository,
    CandidateRecord,
    CandidateRepository,
    EvaluationJob,
    EvaluationRepository,
    EvaluationStatus,
    JobRepository,
    SessionRecord,
    SessionRepository,
)
from schemas.application import ApplicationStatus
from schemas.scoring import CandidateLeaderboard

logger = get_logger("services.recruiter")

# The application statuses that were ever part of the ranking pool - matches
# orchestration/graph.py's own population (`state["shortlisted_candidates"]`).
# SUBMITTED (never matched/decided) and REJECTED are not ranking candidates.
_RANKING_STATUSES = (ApplicationStatus.SHORTLISTED, ApplicationStatus.INTERVIEW_LINKED)


@dataclass
class ApplicationOverviewData:
    """What `list_job_applications`/`get_application_overview` assemble for
    one application - plain data, not a pydantic response model (services
    never import from `api.*` response models; api/routes/*.py maps this
    into `api.models_recruiter.ApplicationOverview`)."""

    application: Application
    candidate: Optional[CandidateRecord]
    session: Optional[SessionRecord]
    evaluation: Optional[EvaluationJob]


@dataclass
class LeaderboardResult:
    leaderboard: CandidateLeaderboard
    total_matching: int
    limit: Optional[int]
    offset: int
    generated_at: datetime


class RecruiterService:
    """Read-only aggregation across Job/Application/Candidate/Session/
    Evaluation, plus the leaderboard ranking view. Holds only repository
    references - constructed per request, like every other service in this
    backend."""

    def __init__(
        self,
        *,
        job_repository: JobRepository,
        application_repository: ApplicationRepository,
        candidate_repository: CandidateRepository,
        session_repository: SessionRepository,
        evaluation_repository: EvaluationRepository,
        leaderboard_factory=None,
    ) -> None:
        self._jobs = job_repository
        self._applications = application_repository
        self._candidates = candidate_repository
        self._sessions = session_repository
        self._evaluations = evaluation_repository
        # The same wiring-seam pattern every other agent-calling service in
        # this backend uses (Chunk 1-4) - None means "construct the real
        # LeaderboardAgent".
        self._leaderboard_factory = leaderboard_factory

    # ------------------------------------------------------------------
    # Applications for a job (Step 3)
    # ------------------------------------------------------------------

    async def list_job_applications(self, job_id: str) -> List[ApplicationOverviewData]:
        await self._require_job(job_id)
        applications = await self._applications.list_for_job(job_id)
        return await self._assemble_overviews(applications)

    async def get_application_overview(self, application_id: str) -> ApplicationOverviewData:
        application = await self._applications.get(application_id)
        if application is None:
            raise NotFoundError(
                "No application found for the given application_id",
                internal_detail=f"application_id={application_id!r} not found",
            )
        overviews = await self._assemble_overviews([application])
        return overviews[0]

    async def _assemble_overviews(
        self, applications: List[Application]
    ) -> List[ApplicationOverviewData]:
        candidate_ids = list({a.candidate_id for a in applications})
        session_ids = [a.session_id for a in applications if a.session_id]

        candidates = await self._candidates.get_many(candidate_ids) if candidate_ids else []
        candidates_by_id = {c.candidate_id: c for c in candidates}

        sessions = await self._sessions.get_many(session_ids) if session_ids else []
        sessions_by_id = {s.session_id: s for s in sessions}

        evaluations_by_session = (
            await self._evaluations.get_many_for_sessions(session_ids) if session_ids else {}
        )

        overviews = []
        for application in applications:
            session = sessions_by_id.get(application.session_id) if application.session_id else None
            evaluation = (
                evaluations_by_session.get(application.session_id) if application.session_id else None
            )
            overviews.append(ApplicationOverviewData(
                application=application,
                candidate=candidates_by_id.get(application.candidate_id),
                session=session,
                evaluation=evaluation,
            ))
        return overviews

    # ------------------------------------------------------------------
    # Leaderboard (Step 8/9)
    # ------------------------------------------------------------------

    async def get_leaderboard(
        self,
        job_id: str,
        *,
        recommendation: Optional[str] = None,
        requires_human_review: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> LeaderboardResult:
        """Rank every application in the job's shortlisted/interview-linked
        pool by its ALREADY-COMPLETED evaluation result - never a
        recomputed score (see module docstring).

        `limit`/`offset` paginate the returned `entries` only; the
        leaderboard's own summary counts (`total_candidates`,
        `strong_candidates`, `requires_review`, `top_candidates`) and every
        entry's `rank`/`percentile_rank` are computed by `LeaderboardAgent`
        over the FULL ranked population first, so they stay meaningful
        regardless of which page or filter is requested.
        """
        await self._require_job(job_id)
        applications = await self._applications.list_for_job(job_id)
        pool = [a for a in applications if a.status in _RANKING_STATUSES]

        session_ids = [a.session_id for a in pool if a.session_id]
        evaluations_by_session = (
            await self._evaluations.get_many_for_sessions(session_ids) if session_ids else {}
        )

        reports = []
        incomplete_candidate_ids: List[str] = []
        for application in pool:
            evaluation = (
                evaluations_by_session.get(application.session_id) if application.session_id else None
            )
            if (
                evaluation is not None
                and evaluation.status == EvaluationStatus.COMPLETED
                and evaluation.result is not None
            ):
                reports.append(evaluation.result)
            else:
                incomplete_candidate_ids.append(application.candidate_id)

        # Explicit, deterministic tie-break (Step 9) - see module docstring.
        reports.sort(key=lambda r: r.candidate_id)

        leaderboard = await self._rank(job_id, reports, incomplete_candidate_ids)

        entries = leaderboard.entries
        if recommendation is not None:
            entries = [e for e in entries if e.recommendation == recommendation]
        if requires_human_review is not None:
            entries = [e for e in entries if e.requires_human_review == requires_human_review]

        total_matching = len(entries)
        entries = entries[offset:]
        if limit is not None:
            entries = entries[:limit]

        return LeaderboardResult(
            leaderboard=leaderboard.model_copy(update={"entries": entries}),
            total_matching=total_matching,
            limit=limit,
            offset=offset,
            generated_at=datetime.now(timezone.utc),
        )

    async def _rank(
        self, job_id: str, reports: list, incomplete_candidate_ids: List[str]
    ) -> CandidateLeaderboard:
        agent = self._leaderboard_factory() if self._leaderboard_factory else LeaderboardAgent()
        agent_result = await agent.run(
            run_id=f"leaderboard_{job_id}", reports=reports, job_id=job_id,
            incomplete_candidates=incomplete_candidate_ids,
        )
        result = (agent_result or {}).get("result") or {}
        leaderboard = result.get("leaderboard")
        if leaderboard is None:
            error = result.get("error") or (agent_result or {}).get("error")
            logger.error(
                "leaderboard generation failed: %s",
                error,
                extra=log_context(event="leaderboard_failed", job_id=job_id),
            )
            raise DependencyError(
                "The leaderboard could not be generated. Please try again.",
                internal_detail=f"LeaderboardAgent failed for job_id={job_id!r}: {error}",
                context={"job_id": job_id},
            )
        return leaderboard

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _require_job(self, job_id: str) -> None:
        """Confirms the job exists so an unknown job_id 404s instead of
        silently returning an empty list/leaderboard indistinguishable from
        "this job genuinely has zero applicants"."""
        record = await self._jobs.get(job_id)
        if record is None:
            raise NotFoundError(
                "No job found for the given job_id",
                internal_detail=f"job_id={job_id!r} not found",
            )


__all__ = ["ApplicationOverviewData", "LeaderboardResult", "RecruiterService"]
