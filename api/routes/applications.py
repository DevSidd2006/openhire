"""
Application endpoints: Candidate -> Application -> Job.

Job-scoped matching (`POST /jobs/{job_id}/match`) and the shortlist view
(`GET /jobs/{job_id}/shortlist`) live in api/routes/jobs.py instead of here,
since both are naturally job-scoped resources - see that module.
"""
from fastapi import APIRouter, Depends, Query

from api.models_applications import (
    ApplicationListResponse,
    ApplicationResponse,
    CreateApplicationRequest,
)
from api.models_recruiter import (
    ApplicationEvaluationResponse,
    ApplicationInterviewResponse,
    InterviewStatusView,
)
from core.dependencies import (
    get_application_service,
    get_candidate_service,
    get_evaluation_service,
    get_interview_service,
)
from core.errors import BadRequestError
from core.security import Principal, require_authenticated, require_scopes
from services.application_service import ApplicationService
from services.candidate_service import CandidateService
from services.evaluation_service import EvaluationService
from services.interview_service import InterviewService

router = APIRouter(prefix="/applications", tags=["applications"])


@router.post("", response_model=ApplicationResponse, status_code=201)
async def create_application(
    payload: CreateApplicationRequest,
    service: ApplicationService = Depends(get_application_service),
    principal: Principal = Depends(require_authenticated),
) -> ApplicationResponse:
    """POST /applications - a candidate applies to a job.

    Errors: 404 `not_found` if the job or candidate doesn't exist; 409
    `conflict` if the job is archived, or if this candidate has already
    applied to this job (Chunk 2 Step 9's duplicate-application case); 403
    `forbidden` if the candidate does not belong to the authenticated user.
    """
    user_id = principal.subject_id or "user_anonymous"
    application = await service.apply(
        job_id=payload.job_id,
        candidate_id=payload.candidate_id,
        user_id=user_id,
    )
    return ApplicationResponse.from_domain(application)


@router.get("/{application_id}", response_model=ApplicationResponse)
async def get_application(
    application_id: str,
    service: ApplicationService = Depends(get_application_service),
    candidate_service: CandidateService = Depends(get_candidate_service),
    principal: Principal = Depends(require_authenticated),
) -> ApplicationResponse:
    """GET /applications/{application_id}. Errors: 404 `not_found`, 403 `forbidden`
    if the application's candidate does not belong to the authenticated user."""
    user_id = principal.subject_id or "user_anonymous"
    application = await service.get_application(application_id)
    await candidate_service.validate_candidate_ownership(
        application.candidate_id, user_id
    )
    return ApplicationResponse.from_domain(application)


@router.get("", response_model=ApplicationListResponse)
async def list_applications(
    job_id: str | None = Query(default=None),
    candidate_id: str | None = Query(default=None),
    service: ApplicationService = Depends(get_application_service),
    candidate_service: CandidateService = Depends(get_candidate_service),
    principal: Principal = Depends(require_authenticated),
) -> ApplicationListResponse:
    """GET /applications?job_id=... or ?candidate_id=... - exactly one of
    the two is required.

    Scoped the same way `SessionRepository.list_for_candidate` is scoped
    (Chunk 1): an unbounded "list every application" is how one job's or one
    candidate's applications end up visible to an unrelated caller, so no
    such listing is offered.

    When filtering by candidate_id, validates that the candidate belongs to
    the authenticated user (403 if not).
    """
    if bool(job_id) == bool(candidate_id):
        raise BadRequestError(
            "Provide exactly one of job_id or candidate_id",
            internal_detail=f"job_id={job_id!r} candidate_id={candidate_id!r}",
        )
    user_id = principal.subject_id or "user_anonymous"
    if job_id:
        applications = await service.list_for_job(job_id)
    else:
        # Validate that the candidate belongs to the authenticated user
        await candidate_service.validate_candidate_ownership(candidate_id, user_id)
        applications = await service.list_for_candidate(candidate_id)
    return ApplicationListResponse.from_domain(applications)


@router.post("/{application_id}/shortlist", response_model=ApplicationResponse)
async def shortlist_application(
    application_id: str,
    service: ApplicationService = Depends(get_application_service),
    principal: Principal = Depends(require_scopes("recruiter:write")),
) -> ApplicationResponse:
    """POST /applications/{application_id}/shortlist - Chunk 5: a
    recruiter's manual override to SHORTLISTED (reversing an automatic
    rejection, or shortlisting ahead of running matching). Does not
    recompute matching - `POST /jobs/{job_id}/match` (Chunk 2, unmodified)
    remains the only thing that invokes `ResumeMatcherAgent`.

    Errors: 404 `not_found`; 409 `conflict` if the application already has
    a linked interview (already past this gate). Idempotent from
    SHORTLISTED itself.
    """
    application = await service.shortlist(application_id)
    return ApplicationResponse.from_domain(application)


@router.post("/{application_id}/reject", response_model=ApplicationResponse)
async def reject_application(
    application_id: str,
    service: ApplicationService = Depends(get_application_service),
    principal: Principal = Depends(require_scopes("recruiter:write")),
) -> ApplicationResponse:
    """POST /applications/{application_id}/reject - Chunk 5: a recruiter's
    decision to reject, at any stage - including after interviewing and
    evaluating the candidate (the most common real use: reviewing a
    completed report and deciding not to proceed). Only changes `status`;
    the interview/transcript/evaluation already on record are untouched
    and remain individually retrievable.

    Errors: 404 `not_found`. Idempotent - rejecting an already-rejected
    application is a no-op success.
    """
    application = await service.reject(application_id)
    return ApplicationResponse.from_domain(application)


@router.get("/{application_id}/interview", response_model=ApplicationInterviewResponse)
async def get_application_interview(
    application_id: str,
    applications: ApplicationService = Depends(get_application_service),
    interviews: InterviewService = Depends(get_interview_service),
    principal: Principal = Depends(require_scopes("recruiter:read")),
) -> ApplicationInterviewResponse:
    """GET /applications/{application_id}/interview - Chunk 5 Step 5: this
    application's interview status, reusing the EXISTING
    `SessionRecord`/`SessionStatus` (Chunk 1/3) - not a second state
    machine.

    `interview=None` means no session has been linked to this application
    yet (still SUBMITTED/SHORTLISTED, or REJECTED before ever
    interviewing) - a normal, valid state, not an error.

    Reads `InterviewService.get_record` (the durable `SessionRepository`
    read), never `get_runner` - a status check must not trigger
    Chunk 3 session RESTORATION, which exists to resume an interview, not
    to answer "what state is it in".

    Errors: 404 `not_found` if the application itself does not exist.
    """
    application = await applications.get_application(application_id)
    interview_status = None
    if application.session_id is not None:
        record = await interviews.get_record(application.session_id)
        interview_status = InterviewStatusView.from_record(record)
    return ApplicationInterviewResponse(application_id=application_id, interview=interview_status)


@router.get("/{application_id}/evaluation", response_model=ApplicationEvaluationResponse)
async def get_application_evaluation(
    application_id: str,
    applications: ApplicationService = Depends(get_application_service),
    evaluations: EvaluationService = Depends(get_evaluation_service),
    principal: Principal = Depends(require_scopes("recruiter:read")),
) -> ApplicationEvaluationResponse:
    """GET /applications/{application_id}/evaluation - the
    application-centric equivalent of Chunk 4's
    `GET /sessions/{session_id}/evaluation`, reusing the EXACT SAME
    `EvaluationService`/`EvaluationJob` - not a second evaluation state
    system, just a different, recruiter-natural lookup path onto the same
    data (Job -> Applications -> Application -> Evaluation).

    `evaluation=None` means no interview has been linked yet, or one has
    but its evaluation has not been triggered yet (e.g. the transcript has
    not finished persisting - see Chunk 4's `transcript_persisted` gate).
    Never returns a completed-looking result while evaluation is actually
    PENDING/RUNNING/FAILED (Step 18) - the real, current `status` on
    `EvaluationJob` is always what is returned.

    Errors: 404 `not_found` if the application itself does not exist.
    """
    application = await applications.get_application(application_id)
    evaluation = None
    if application.session_id is not None:
        evaluation = await evaluations.get_evaluation_for_session(application.session_id)
    return ApplicationEvaluationResponse(application_id=application_id, evaluation=evaluation)
