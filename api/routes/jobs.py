"""
Job endpoints.

Thin translators, exactly like api/routes/interview.py: validate the
request -> call exactly one `JobService` method -> shape the response. No
job-description extraction logic lives here - that stays inside
`agents/jd_analyzer/agent.py` via `services/job_service.py`.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query

from api.models_applications import ApplicationListResponse, MatchingRunResponse
from api.models_jobs import (
    CreateJobRequest,
    JobListResponse,
    JobResponse,
    UpdateJobRequest,
)
from api.models_recruiter import (
    ApplicationOverview,
    ApplicationOverviewListResponse,
    CandidateSummaryView,
    InterviewStatusView,
    LeaderboardResponse,
)
from core.dependencies import (
    get_application_service,
    get_job_service,
    get_recruiter_service,
)
from core.security import Principal, require_authenticated, require_scopes
from services.application_service import ApplicationService
from services.job_service import JobService
from services.recruiter_service import RecruiterService

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", response_model=JobResponse, status_code=201)
async def create_job(
    payload: CreateJobRequest,
    service: JobService = Depends(get_job_service),
    principal: Principal = Depends(require_authenticated),
) -> JobResponse:
    """POST /jobs - analyze raw job-description text (via the existing
    `JDAnalyzerAgent`) into a structured job posting and store it.

    Errors: 503 `dependency_unavailable` if analysis fails (core/errors.py:
    `DependencyError`); 422 if `description` is empty.

    Authorization: `require_authenticated` only - inert while
    `AUTH_ENABLED=false` (core/security.py). Recruiter-only enforcement
    needs real role persistence, which is blocked on the database/auth
    teammate; see the Chunk 2 handoff.
    """
    record = await service.create_job(
        description=payload.description,
        job_id=payload.job_id,
        openings=payload.openings,
        is_practice=payload.is_practice,
        created_by_user_id=(principal.subject_id or "user_anonymous") if payload.is_practice else None,
    )
    return JobResponse.from_record(record)


@router.get("/practice/mine", response_model=JobListResponse)
async def list_my_practice_jobs(
    service: JobService = Depends(get_job_service),
    principal: Principal = Depends(require_authenticated),
) -> JobListResponse:
    """GET /jobs/practice/mine - the calling candidate's own practice jobs
    (mock-interview JDs they created for themselves), newest first. Never
    another user's - see JobService.list_practice_jobs_for_user."""
    user_id = principal.subject_id or "user_anonymous"
    records = await service.list_practice_jobs_for_user(user_id)
    return JobListResponse.from_records(records)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    service: JobService = Depends(get_job_service),
    principal: Principal = Depends(require_authenticated),
) -> JobResponse:
    """GET /jobs/{job_id}. Errors: 404 `not_found` if no such job."""
    record = await service.get_job(job_id)
    return JobResponse.from_record(record)


@router.get("", response_model=JobListResponse)
async def list_jobs(
    include_archived: bool = Query(default=False),
    service: JobService = Depends(get_job_service),
    principal: Principal = Depends(require_authenticated),
) -> JobListResponse:
    """GET /jobs - open postings by default; pass `include_archived=true`
    to also see archived ones."""
    records = await service.list_jobs(include_archived=include_archived)
    return JobListResponse.from_records(records)


@router.patch("/{job_id}", response_model=JobResponse)
async def update_job(
    job_id: str,
    payload: UpdateJobRequest,
    service: JobService = Depends(get_job_service),
    principal: Principal = Depends(require_authenticated),
) -> JobResponse:
    """PATCH /jobs/{job_id} - partial field edit; does not re-run analysis
    (see services/job_service.py:update_job).

    Errors: 404 `not_found`; 409 `conflict` if the job is archived; 422 if
    the edited fields fail `JobDescription`'s own validation (e.g.
    competency weights no longer summing to 1.0).
    """
    record = await service.update_job(job_id, payload.model_dump(exclude_unset=True))
    return JobResponse.from_record(record)


@router.post("/{job_id}/archive", response_model=JobResponse)
async def archive_job(
    job_id: str,
    service: JobService = Depends(get_job_service),
    principal: Principal = Depends(require_authenticated),
) -> JobResponse:
    """POST /jobs/{job_id}/archive - soft-delete; idempotent. Errors: 404
    `not_found`."""
    record = await service.archive_job(job_id)
    return JobResponse.from_record(record)


@router.post("/{job_id}/match", response_model=MatchingRunResponse)
async def match_job(
    job_id: str,
    service: ApplicationService = Depends(get_application_service),
    principal: Principal = Depends(require_authenticated),
) -> MatchingRunResponse:
    """POST /jobs/{job_id}/match - run matching (the existing
    `ResumeMatcherAgent`) for every application still awaiting a decision
    on this job, then return every application for the job ranked by match
    score (Step 6: Job -> Applications -> Matching -> Ranked candidates).

    A single candidate's matching failure does not fail the whole run - it
    is skipped and named in `failed_candidate_ids`, mirroring
    `orchestration/graph.py:node_match_resumes`'s existing resilience
    contract. Errors: 404 `not_found` if the job does not exist.
    """
    result = await service.run_matching_for_job(job_id)
    return MatchingRunResponse(
        job_id=result.job_id,
        matched=result.matched_count,
        shortlisted=result.shortlisted_count,
        rejected=result.rejected_count,
        failed_candidate_ids=result.errors,
        applications=result.applications,
    )


@router.get("/{job_id}/shortlist", response_model=ApplicationListResponse)
async def get_shortlist(
    job_id: str,
    service: ApplicationService = Depends(get_application_service),
    principal: Principal = Depends(require_authenticated),
) -> ApplicationListResponse:
    """GET /jobs/{job_id}/shortlist - ranked, shortlisted applications for
    this job (Step 6's final "Shortlisted candidates" stage)."""
    applications = await service.get_shortlist(job_id)
    return ApplicationListResponse.from_domain(applications)


@router.get("/{job_id}/applications", response_model=ApplicationOverviewListResponse)
async def list_job_applications(
    job_id: str,
    service: RecruiterService = Depends(get_recruiter_service),
    principal: Principal = Depends(require_scopes("recruiter:read")),
) -> ApplicationOverviewListResponse:
    """GET /jobs/{job_id}/applications - Chunk 5's recruiter view: every
    application for this job (any status - not only shortlisted, unlike
    `/shortlist` above), each row already carrying the candidate summary,
    interview status and evaluation status, assembled in one batched pass
    (`RecruiterService`) rather than one request per row per resource.

    Distinct from `GET /applications?job_id=...` (Chunk 2), which returns
    the bare `Application` record only - this is the richer, recruiter-
    specific view Chunk 5 Step 3 calls for, not a duplicate of that route.

    Authorization: `require_scopes("recruiter:read")` - inert while
    `AUTH_ENABLED=false`, exactly like `require_authenticated` elsewhere in
    this backend, but names the intended future role explicitly (see
    core/security.py and this backend's Chunk 5 handoff for what remains
    to enforce it for real). Errors: 404 `not_found` if the job itself does
    not exist (an unknown job_id must not look identical to "zero
    applicants").
    """
    overviews = await service.list_job_applications(job_id)
    views = [
        ApplicationOverview(
            application=o.application,
            candidate=CandidateSummaryView.from_record(o.candidate) if o.candidate else None,
            interview_status=InterviewStatusView.from_record(o.session) if o.session else None,
            evaluation_id=o.evaluation.evaluation_id if o.evaluation else None,
            evaluation_status=o.evaluation.status if o.evaluation else None,
        )
        for o in overviews
    ]
    return ApplicationOverviewListResponse.from_domain(job_id, views)


@router.get("/{job_id}/leaderboard", response_model=LeaderboardResponse)
async def get_job_leaderboard(
    job_id: str,
    recommendation: Optional[str] = Query(default=None),
    requires_human_review: Optional[bool] = Query(default=None),
    limit: Optional[int] = Query(default=None, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: RecruiterService = Depends(get_recruiter_service),
    principal: Principal = Depends(require_scopes("recruiter:read")),
) -> LeaderboardResponse:
    """GET /jobs/{job_id}/leaderboard - candidates ranked by their
    ALREADY-COMPLETED evaluation, via the EXISTING `LeaderboardAgent`
    (agents/leaderboard/agent.py) - never a recomputed score (Chunk 5
    Step 17: the same `weighted_final_score` visible on
    `GET /evaluations/{evaluation_id}` is what is ranked here).

    Optional filters (`recommendation`, `requires_human_review`) and
    pagination (`limit`/`offset`) narrow the returned `entries` only - the
    leaderboard's own summary counts and `top_candidates` always describe
    the full, unfiltered ranked population (see
    services/recruiter_service.py's docstring).

    Errors: 404 `not_found` if the job does not exist; 503
    `dependency_unavailable` if `LeaderboardAgent` itself fails (mirrors
    every other agent-calling endpoint in this backend).
    """
    result = await service.get_leaderboard(
        job_id, recommendation=recommendation, requires_human_review=requires_human_review,
        limit=limit, offset=offset,
    )
    return LeaderboardResponse(
        leaderboard=result.leaderboard, total_matching=result.total_matching,
        limit=result.limit, offset=result.offset, generated_at=result.generated_at,
    )
