"""
Interview session endpoints.

Every handler below is a thin translator: validate the request -> call
exactly one `InterviewService` method -> shape the response. No competency
prioritization, no question generation, no answer evaluation, no evidence
construction and no scoring happens in this file - all of that stays inside
utils/interview_session.py, utils/adaptive_interview.py,
agents/interviewer/agent.py and utils/evidence.py exactly as P4 built them.

What changed in this chunk: the handlers previously reached into
`request.app.state` for the registry and the interviewer factory, and owned
the decisions about what gets logged and recorded. Those are application
concerns and now live in `services/interview_service.py`; the handlers
declare what they need through `Depends(...)`, which puts each handler's
dependencies in its signature and makes them overridable per-test.

The routes, their methods, their request bodies and their response shapes
are all unchanged.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Request

from api.errors import InvalidRequestError
from core.errors import ConflictError
from api.models import (
    CreateSessionRequest,
    CreateSessionResponse,
    FinishSessionResponse,
    ProgressView,
    QuestionView,
    SessionStateResponse,
    SubmitAnswerRequest,
    SubmitAnswerResponse,
)
from api.models_evaluations import SessionEvaluationResponse
from core.dependencies import (
    get_application_service,
    get_evaluation_service,
    get_interview_service,
)
from core.security import Principal, require_authenticated
from repositories.interfaces import EvaluationJob
from services.application_service import ApplicationService
from services.evaluation_service import EvaluationService
from services.interview_service import InterviewService
from utils.interview_session import SessionStatus

router = APIRouter(prefix="/sessions", tags=["interview"])


async def _trigger_evaluation_if_ready(
    evaluations: EvaluationService, *, session_id: str, status: SessionStatus,
    transcript_persisted: Optional[bool],
) -> Optional[EvaluationJob]:
    """Chunk 4's interview-completion integration point (Step 12).

    Called from every place a session's sealed/persisted state can change
    (submit_answer, finish_session, get_session_state, and the voice
    transport in api/routes/voice.py) with whatever that call site already
    knows locally - never re-fetches the session, never raises. Evaluation
    is only ever triggered once the transcript is CONFIRMED persisted
    (transcript_persisted is True): evaluating a transcript that might not
    survive a restart would be evaluating the wrong source of truth. If it
    is not (still False, or the session isn't sealed at all), this is
    silently a no-op - the same opportunistic retry-on-next-read pattern
    Chunk 1 already established for the transcript itself, extended one
    step further to evaluation.

    `EvaluationService.trigger_evaluation` is itself idempotent (Chunk 4
    Step 6), so calling this from multiple call sites for the same session
    is always safe - at most one evaluation job is ever created.
    """
    if status != SessionStatus.SEALED or not transcript_persisted:
        return None
    return await evaluations.trigger_evaluation(session_id)


@router.post("", response_model=CreateSessionResponse, status_code=201)
async def create_session(
    payload: CreateSessionRequest,
    request: Request,
    service: InterviewService = Depends(get_interview_service),
    applications: ApplicationService = Depends(get_application_service),
    principal: Principal = Depends(require_authenticated),
) -> CreateSessionResponse:
    """POST /sessions - create an interview session and ask the first
    question. Which competency and what question is decided entirely by the
    P3 adaptive engine via `InterviewSessionRunner.start()`.

    `require_authenticated` is inert while AUTH_ENABLED=false (every request
    is the anonymous principal, exactly as today). It is attached now so
    that enabling authentication is a configuration change plus an
    AuthProvider, not an edit to every route - see core/security.py.

    Chunk 2 bridge: `payload.application_id` is optional and additive (see
    api/models.py). Omitted, every line below the mismatch checks is
    byte-for-byte what this handler already did. Supplied, the named
    application is validated against this same request and linked to the
    resulting session AFTER it is created - a failure to link never
    prevents an otherwise-valid interview from starting; it is
    validated up front instead (see the ConflictError/NotFoundError paths)
    precisely so that it cannot fail post-creation.
    """
    if payload.job_description.job_id != payload.job_id:
        raise InvalidRequestError("job_id does not match job_description.job_id")
    if payload.parsed_resume.candidate_id != payload.candidate_id:
        raise InvalidRequestError("candidate_id does not match parsed_resume.candidate_id")

    if payload.application_id is not None:
        application = await applications.get_application(payload.application_id)
        if application.job_id != payload.job_id or application.candidate_id != payload.candidate_id:
            raise InvalidRequestError(
                "application_id does not match job_id/candidate_id"
            )
        # Fails fast, before the session (and any LLM call it makes) is
        # created, rather than creating a session that then can't be linked.
        if application.status.value != "shortlisted":
            raise ConflictError(
                "An application must be shortlisted before an interview can be linked to it",
                internal_detail=f"application_id={payload.application_id!r} status={application.status.value!r}",
            )

    session_id, runner = await service.create_session(
        job_description=payload.job_description,
        parsed_resume=payload.parsed_resume,
        candidate_id=payload.candidate_id,
        max_questions=payload.max_questions,
        application_id=payload.application_id,
    )
    # Available to the InterviewSessionError handler, which needs the
    # runner's status to choose between 409 and 500 (api/errors.py).
    request.state.runner = runner

    linked_application_id = None
    if payload.application_id is not None:
        await applications.link_interview_session(payload.application_id, session_id)
        linked_application_id = payload.application_id

    return CreateSessionResponse(
        session_id=session_id,
        status=runner.status,
        current_question=QuestionView.from_domain(runner.get_current_question()),
        application_id=linked_application_id,
    )


@router.get("/{session_id}", response_model=SessionStateResponse)
async def get_session_state(
    session_id: str,
    request: Request,
    service: InterviewService = Depends(get_interview_service),
    evaluations: EvaluationService = Depends(get_evaluation_service),
    principal: Principal = Depends(require_authenticated),
) -> SessionStateResponse:
    """GET /sessions/{id} - a controlled snapshot of session state. Never
    returns the raw InterviewState or the transcript.

    If the session is sealed and its transcript previously failed to
    persist, this is also the retry point: `retry_transcript_persistence`
    is a safe no-op once the transcript is already stored, so calling it on
    every read of a sealed session costs nothing once it has succeeded (see
    services/interview_service.py). Chunk 4: the same read is also the
    retry point for evaluation itself, in case it could not be triggered
    at seal time because the transcript write had not yet succeeded.
    """
    runner = await service.get_runner(session_id)
    request.state.runner = runner
    state = runner.get_state()

    transcript_persisted = await service.retry_transcript_persistence(session_id, runner)
    evaluation_job = await _trigger_evaluation_if_ready(
        evaluations, session_id=session_id, status=runner.status,
        transcript_persisted=transcript_persisted,
    )

    return SessionStateResponse(
        session_id=session_id,
        status=runner.status,
        current_question=QuestionView.from_domain(runner.get_current_question()),
        progress=ProgressView.from_domain(state),
        termination_reason=state.termination_reason,
        transcript_persisted=transcript_persisted,
        evaluation_id=evaluation_job.evaluation_id if evaluation_job else None,
        evaluation_status=evaluation_job.status if evaluation_job else None,
    )


@router.post("/{session_id}/answers", response_model=SubmitAnswerResponse)
async def submit_answer(
    session_id: str,
    payload: SubmitAnswerRequest,
    request: Request,
    service: InterviewService = Depends(get_interview_service),
    evaluations: EvaluationService = Depends(get_evaluation_service),
    principal: Principal = Depends(require_authenticated),
) -> SubmitAnswerResponse:
    """POST /sessions/{id}/answers - the candidate's answer is untrusted
    content; it is passed straight through to
    `InterviewSessionRunner.submit_answer()`, which is the ONLY place that
    evaluates it, builds evidence and decides what happens next. Neither
    this handler nor the service inspects or interprets the answer text.

    Chunk 4: if this turn seals the session AND its transcript persists
    successfully, evaluation is triggered here (Step 12) - scheduled in the
    background (Step 5), never awaited, so this request never waits on the
    multi-agent evaluation pipeline.
    """
    # Resolved before submitting so the error handler has the runner even if
    # submit_answer() is what fails.
    runner = await service.get_runner(session_id)
    request.state.runner = runner

    result = await service.submit_answer(session_id, payload.answer_text)

    response = SubmitAnswerResponse.from_domain(result)
    # `runner` is the same object submit_answer() just advanced, so this
    # reflects the outcome of the persistence attempt this call just made -
    # never a stale or optimistic value (see services/interview_service.py).
    response.transcript_persisted = await service.get_transcript_persistence_status(runner)

    evaluation_job = await _trigger_evaluation_if_ready(
        evaluations, session_id=session_id, status=result.session_status,
        transcript_persisted=response.transcript_persisted,
    )
    if evaluation_job is not None:
        response.evaluation_id = evaluation_job.evaluation_id
        response.evaluation_status = evaluation_job.status
    return response


@router.post("/{session_id}/finish", response_model=FinishSessionResponse)
async def finish_session(
    session_id: str,
    request: Request,
    service: InterviewService = Depends(get_interview_service),
    evaluations: EvaluationService = Depends(get_evaluation_service),
    principal: Principal = Depends(require_authenticated),
) -> FinishSessionResponse:
    """POST /sessions/{id}/finish - request explicit termination. Delegates
    entirely to `InterviewSessionRunner.request_finish()`, which routes
    through the same decide/validate/seal path as any other termination -
    this endpoint cannot bypass termination rules.

    Chunk 4: same evaluation trigger as `submit_answer` above, for the case
    where the interview ends via an explicit finish rather than
    auto-termination.
    """
    runner = await service.get_runner(session_id)
    request.state.runner = runner

    state = await service.finish_session(session_id)

    transcript_persisted = await service.get_transcript_persistence_status(runner)
    evaluation_job = await _trigger_evaluation_if_ready(
        evaluations, session_id=session_id, status=runner.status,
        transcript_persisted=transcript_persisted,
    )

    return FinishSessionResponse(
        session_id=session_id,
        status=runner.status,
        termination_reason=state.termination_reason,
        transcript_persisted=transcript_persisted,
        evaluation_id=evaluation_job.evaluation_id if evaluation_job else None,
        evaluation_status=evaluation_job.status if evaluation_job else None,
    )


@router.get("/{session_id}/evaluation", response_model=SessionEvaluationResponse)
async def get_session_evaluation(
    session_id: str,
    service: InterviewService = Depends(get_interview_service),
    evaluations: EvaluationService = Depends(get_evaluation_service),
    principal: Principal = Depends(require_authenticated),
) -> SessionEvaluationResponse:
    """GET /sessions/{id}/evaluation - this session's evaluation job, if one
    has been triggered.

    Nested under `/sessions/{id}` (this backend's existing convention -
    session_id is the public, capability-token identifier every other
    session-scoped endpoint already uses), rather than a new
    `/interviews/{interview_id}/...` resource this API does not otherwise
    have. `GET /evaluations/{evaluation_id}` (api/routes/evaluations.py) is
    the other way to reach the same data, once `evaluation_id` is known
    (e.g. from this endpoint, or from submit_answer/finish's response).

    Errors: 404 `not_found` if the SESSION itself does not exist. If the
    session exists but no evaluation has been triggered yet (still in
    progress, or its transcript has not finished persisting), this is a
    200 with `evaluation: null` - a normal, valid state, not an error (see
    api/models_evaluations.py:SessionEvaluationResponse).
    """
    await service.get_runner(session_id)  # 404s if the session itself is unknown
    evaluation_job = await evaluations.get_evaluation_for_session(session_id)
    return SessionEvaluationResponse(session_id=session_id, evaluation=evaluation_job)
