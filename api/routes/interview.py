"""
P5 Phase 5/6/7/12: interview session endpoints.

Every handler below is a thin translator: parse request -> call exactly one
InterviewSessionRunner/SessionRegistry method -> shape the response. No
competency prioritization, no question generation, no answer evaluation, no
evidence construction, and no scoring happens in this file - all of that
stays inside utils/interview_session.py, utils/adaptive_interview.py,
agents/interviewer/agent.py, and utils/evidence.py exactly as P4 built them.
"""
from fastapi import APIRouter, Depends, Request

from api.errors import InvalidRequestError
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
from api.registry import SessionRegistry
from utils.interview_session import InterviewSessionRunner
from utils.logging import get_logger

router = APIRouter(prefix="/sessions", tags=["interview"])
logger = get_logger("api.interview")


def get_registry(request: Request) -> SessionRegistry:
    return request.app.state.registry


@router.post("", response_model=CreateSessionResponse, status_code=201)
async def create_session(
    payload: CreateSessionRequest,
    request: Request,
    registry: SessionRegistry = Depends(get_registry),
) -> CreateSessionResponse:
    """POST /sessions - create an interview session and ask the first
    question (P5 Phase 6). All of "which competency", "what question" is
    decided by InterviewSessionRunner.start() via the P3 adaptive engine -
    this handler only validates the request and stores the resulting
    runner."""
    if payload.job_description.job_id != payload.job_id:
        raise InvalidRequestError(
            "job_id does not match job_description.job_id"
        )
    if payload.parsed_resume.candidate_id != payload.candidate_id:
        raise InvalidRequestError(
            "candidate_id does not match parsed_resume.candidate_id"
        )

    interviewer_factory = getattr(request.app.state, "interviewer_factory", None)
    runner = InterviewSessionRunner(
        payload.job_description,
        payload.parsed_resume,
        candidate_id=payload.candidate_id,
        max_questions=payload.max_questions,
        interviewer=interviewer_factory() if interviewer_factory else None,
    )
    request.state.runner = runner  # available to the error handler if start() fails
    await runner.start()

    session_id = await registry.create_session(runner)
    logger.info(
        f"session created: session_id={session_id} candidate_id={payload.candidate_id} "
        f"job_id={payload.job_id}"
    )
    if runner.get_current_question() is not None:
        logger.info(
            f"question presented: session_id={session_id} "
            f"question_id={runner.get_current_question().question_id} "
            f"competency={runner.get_current_question().competency}"
        )

    return CreateSessionResponse(
        session_id=session_id,
        status=runner.status,
        current_question=QuestionView.from_domain(runner.get_current_question()),
    )


@router.get("/{session_id}", response_model=SessionStateResponse)
async def get_session_state(
    session_id: str,
    registry: SessionRegistry = Depends(get_registry),
) -> SessionStateResponse:
    """GET /sessions/{id} - a controlled snapshot of session state
    (P5 Phase 4/11). Never returns the raw InterviewState or transcript."""
    runner = await registry.get_session(session_id)
    state = runner.get_state()
    return SessionStateResponse(
        session_id=session_id,
        status=runner.status,
        current_question=QuestionView.from_domain(runner.get_current_question()),
        progress=ProgressView.from_domain(state),
        termination_reason=state.termination_reason,
    )


@router.post("/{session_id}/answers", response_model=SubmitAnswerResponse)
async def submit_answer(
    session_id: str,
    payload: SubmitAnswerRequest,
    request: Request,
    registry: SessionRegistry = Depends(get_registry),
) -> SubmitAnswerResponse:
    """POST /sessions/{id}/answers - the candidate's answer is untrusted
    content; it is passed straight through to
    InterviewSessionRunner.submit_answer() (P4), which is the ONLY place
    that evaluates it, builds evidence, and decides what happens next
    (P5 Phase 7). This handler never inspects/interprets the answer text
    itself."""
    runner = await registry.get_session(session_id)
    request.state.runner = runner

    result = await runner.submit_answer(payload.answer_text)

    logger.info(
        f"answer submitted: session_id={session_id} question_id={result.question.question_id} "
        f"competency={result.question.competency} answer_length={len(payload.answer_text)}"
    )
    if result.session_status.value == "sealed":
        logger.info(
            f"interview finished: session_id={session_id} termination_reason={result.termination_reason}"
        )

    return SubmitAnswerResponse.from_domain(result)


@router.post("/{session_id}/finish", response_model=FinishSessionResponse)
async def finish_session(
    session_id: str,
    request: Request,
    registry: SessionRegistry = Depends(get_registry),
) -> FinishSessionResponse:
    """POST /sessions/{id}/finish - request explicit termination
    (P5 Phase 12). Delegates entirely to
    InterviewSessionRunner.request_finish(), which routes through the same
    decide/validate/seal path as any other termination - this endpoint
    cannot bypass termination rules."""
    runner = await registry.get_session(session_id)
    request.state.runner = runner

    state = await runner.request_finish()

    logger.info(
        f"interview finished (explicit): session_id={session_id} "
        f"termination_reason={state.termination_reason}"
    )

    return FinishSessionResponse(
        session_id=session_id,
        status=runner.status,
        termination_reason=state.termination_reason,
    )
