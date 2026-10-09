"""REST lifecycle and durable control channel for Gemini Live interviews."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from api.errors import InvalidRequestError
from api.models import CreateSessionRequest
from api.models_live_interview import (
    CONTROL_MESSAGE_MODELS,
    ControlAuthenticate,
    ControlCompletionRequest,
    ControlLifecycle,
    ControlMetric,
    ControlProgress,
    ControlResumptionHandle,
    ControlTranscriptFinal,
    LiveFinishRequest,
    LiveFinishResponse,
    LiveAudioTestTokenResponse,
    LiveSessionResponse,
    LiveTokenRequest,
    LiveTokenResponse,
)
from core.config import AppSettings
from core.container import ServiceContainer
from core.dependencies import (
    build_app_settings,
    build_evaluation_service,
    build_gemini_live_token_service,
    build_live_interview_service,
    get_app_settings,
    get_application_service,
    get_candidate_service,
    get_container,
    get_evaluation_service,
    get_gemini_live_token_service,
    get_live_interview_service,
)
from core.errors import (
    ConflictError,
    GeminiLiveDisabledError,
    GeminiLiveUnconfiguredError,
    NotFoundError,
    UnauthorizedError,
)
from core.security import Principal, require_authenticated
from repositories.interfaces import SessionRecord
from schemas.application import ApplicationStatus
from schemas.job import Competency, JobDescription
from schemas.live_interview import LiveInterviewStatus
from schemas.resume import ParsedResume
from schemas.rubric import AnchoredCompetency, JobRubric, RubricStatus
from services.application_service import ApplicationService
from services.candidate_service import CandidateService
from services.evaluation_service import EvaluationService
from services.gemini_live_token import GeminiLiveTokenService
from services.live_interview_service import LiveInterviewService


router = APIRouter(tags=["live-interview"])


@router.post("/live-audio-test/token", response_model=LiveAudioTestTokenResponse)
async def issue_live_audio_test_token(
    tokens: GeminiLiveTokenService = Depends(get_gemini_live_token_service),
    settings: AppSettings = Depends(get_app_settings),
) -> LiveAudioTestTokenResponse:
    """Mint a context-free Gemini Live token for the local audio playground."""
    if settings.environment not in {"development", "test"}:
        raise NotFoundError("The live audio test was not found.")
    if not settings.gemini_live_enabled:
        raise GeminiLiveDisabledError()
    issued = await tokens.issue_audio_test()
    return LiveAudioTestTokenResponse(**issued.__dict__)


def _demo_competency(name: str, definition: str, weight: float) -> AnchoredCompetency:
    return AnchoredCompetency(
        name=name,
        definition=definition,
        weight=weight,
        anchors={
            1: "No relevant evidence provided.",
            2: "Limited evidence with substantial gaps.",
            3: "Adequate evidence for routine situations.",
            4: "Strong evidence with clear ownership and judgment.",
            5: "Exceptional evidence with measurable impact and depth.",
        },
    )


def _demo_context() -> tuple[JobDescription, ParsedResume, JobRubric]:
    """Small, deterministic context for exercising only the live panel."""
    job_id = "job_local_live_demo"
    candidate_id = "candidate_local_live_demo"
    competencies = [
        Competency(name="Problem solving", weight=0.34, importance="critical"),
        Competency(name="Technical communication", weight=0.33, importance="high"),
        Competency(name="Software design", weight=0.33, importance="high"),
    ]
    job = JobDescription(
        job_id=job_id,
        title="Software Engineer",
        description=(
            "A local realtime interview used to exercise the OpenHire voice "
            "panel, interruption handling, captions, and session resumption."
        ),
        required_skills=["Problem solving", "Software design", "Communication"],
        competencies=competencies,
        interview_topics=["problem solving", "system design", "project experience"],
    )
    resume = ParsedResume(
        candidate_id=candidate_id,
        candidate_name="Local Candidate",
        summary="Software engineer testing the realtime interview experience.",
        skills=["Python", "APIs", "Databases", "System design"],
        raw_text=(
            "Local Candidate is a software engineer with experience building "
            "APIs and reliable backend services."
        ),
    )
    rubric = JobRubric(
        rubric_id="rubric_local_live_demo",
        job_id=job_id,
        version=1,
        status=RubricStatus.APPROVED,
        competencies=[
            _demo_competency(
                "Problem solving", "Breaks down ambiguous technical problems.", 0.34
            ),
            _demo_competency(
                "Technical communication", "Explains decisions clearly.", 0.33
            ),
            _demo_competency(
                "Software design", "Designs maintainable software systems.", 0.33
            ),
        ],
    )
    return job, resume, rubric


def _response(
    record: SessionRecord, reconnect_after_seconds: int = 540
) -> LiveSessionResponse:
    state = record.live_state
    assert state is not None
    assert record.interview_id is not None
    return LiveSessionResponse(
        session_id=record.session_id,
        interview_id=record.interview_id,
        status=state.status,
        application_id=record.application_id,
        last_sequence=state.last_sequence,
        wrap_up_at=state.wrap_up_at,
        hard_stop_at=state.hard_stop_at,
        reconnect_after_seconds=reconnect_after_seconds,
    )


async def _enforce_ownership(
    record: SessionRecord,
    principal: Principal,
    candidates: CandidateService,
    *,
    allow_recruiter: bool,
) -> None:
    if allow_recruiter and principal.has_scopes(["recruiter:read"]):
        return
    user_id = principal.subject_id or "user_anonymous"
    try:
        await candidates.validate_candidate_ownership(record.candidate_id, user_id)
    except NotFoundError:
        # Preserve the existing session route's behavior for directly seeded
        # application/session fixtures that have no candidate record.
        return


async def _ensure_configured_before_creation(
    *,
    request: Request,
    settings: AppSettings,
    container: ServiceContainer,
    recruiter_user_id: str | None,
) -> None:
    if not settings.gemini_live_enabled:
        raise GeminiLiveDisabledError()
    if container.gemini_live_token_factory_for(request.app) is not None:
        return
    if settings.gemini_api_key:
        return
    if not recruiter_user_id or not settings.byok_encryption_key:
        raise GeminiLiveUnconfiguredError()

    from services.llm_credential_service import LLMCredentialService

    credentials = LLMCredentialService(
        credential_repository=container.llm_credential_repository,
        encryption_key=settings.byok_encryption_key,
    )
    if not await credentials.resolve_api_key_for_provider(recruiter_user_id, "gemini"):
        raise GeminiLiveUnconfiguredError()


@router.post("/live-sessions", response_model=LiveSessionResponse, status_code=201)
async def create_live_session(
    payload: CreateSessionRequest,
    request: Request,
    live: LiveInterviewService = Depends(get_live_interview_service),
    applications: ApplicationService = Depends(get_application_service),
    candidates: CandidateService = Depends(get_candidate_service),
    container: ServiceContainer = Depends(get_container),
    settings: AppSettings = Depends(get_app_settings),
    principal: Principal = Depends(require_authenticated),
) -> LiveSessionResponse:
    if payload.job_description.job_id != payload.job_id:
        raise InvalidRequestError("job_id does not match job_description.job_id")
    if payload.parsed_resume.candidate_id != payload.candidate_id:
        raise InvalidRequestError(
            "candidate_id does not match parsed_resume.candidate_id"
        )

    job_record = await container.job_repository.get(payload.job_id)
    recruiter_user_id = (
        job_record.created_by_user_id if job_record is not None else None
    )
    await _ensure_configured_before_creation(
        request=request,
        settings=settings,
        container=container,
        recruiter_user_id=recruiter_user_id,
    )

    if payload.application_id is not None:
        application = await applications.get_application(payload.application_id)
        if (
            application.job_id != payload.job_id
            or application.candidate_id != payload.candidate_id
        ):
            raise InvalidRequestError(
                "application_id does not match job_id/candidate_id"
            )
        await candidates.validate_candidate_ownership(
            application.candidate_id, principal.subject_id or "user_anonymous"
        )
        if application.status.value not in ("shortlisted", "interview_linked"):
            raise ConflictError(
                "An application must be shortlisted before an interview can be linked to it"
            )
        if (
            application.status == ApplicationStatus.INTERVIEW_LINKED
            and application.session_id is not None
        ):
            existing = await container.session_repository.get(application.session_id)
            if existing is not None and existing.status.value != "failed":
                if existing.interview_mode != "gemini_live" or existing.live_state is None:
                    raise ConflictError(
                        "This application is already linked to a turn-based interview."
                    )
                return _response(existing, reconnect_after_seconds=settings.gemini_live_reconnect_seconds)
    else:
        user_id = principal.subject_id or "user_anonymous"
        try:
            await candidates.validate_candidate_ownership(payload.candidate_id, user_id)
        except NotFoundError:
            pass

    rubric = await container.rubric_repository.get_approved_for_job(payload.job_id)
    if rubric is None:
        raise ConflictError(
            "An approved rubric is required before starting a realtime interview."
        )

    candidate_record = await container.candidate_repository.get(payload.candidate_id)
    job_desc = (
        job_record.job_description
        if job_record is not None and getattr(job_record, "job_description", None) is not None
        else payload.job_description
    )
    parsed_res = (
        candidate_record.parsed_resume
        if candidate_record is not None and getattr(candidate_record, "parsed_resume", None) is not None
        else payload.parsed_resume
    )

    record = await live.create_session(
        job_description=job_desc,
        parsed_resume=parsed_res,
        rubric_snapshot=rubric,
        candidate_id=payload.candidate_id,
        application_id=payload.application_id,
    )
    if payload.application_id is not None:
        await applications.link_interview_session(
            payload.application_id, record.session_id
        )
    return _response(record, reconnect_after_seconds=settings.gemini_live_reconnect_seconds)


@router.post(
    "/live-sessions/demo", response_model=LiveSessionResponse, status_code=201
)
async def create_demo_live_session(
    request: Request,
    live: LiveInterviewService = Depends(get_live_interview_service),
    container: ServiceContainer = Depends(get_container),
    settings: AppSettings = Depends(get_app_settings),
) -> LiveSessionResponse:
    """Create an isolated local session without the hiring pipeline.

    This endpoint is intentionally unavailable in staging and production.
    It persists only in the configured session repository and carries no
    application link, so completing it seals the transcript without starting
    the evaluation pipeline.
    """
    if settings.environment not in {"development", "test"}:
        raise NotFoundError("The live interview session was not found.")
    await _ensure_configured_before_creation(
        request=request,
        settings=settings,
        container=container,
        recruiter_user_id=None,
    )
    job, resume, rubric = _demo_context()
    record = await live.create_session(
        job_description=job,
        parsed_resume=resume,
        rubric_snapshot=rubric,
        candidate_id=resume.candidate_id,
    )
    return _response(record, reconnect_after_seconds=settings.gemini_live_reconnect_seconds)


@router.get("/live-sessions/{session_id}", response_model=LiveSessionResponse)
async def get_live_session(
    session_id: str,
    live: LiveInterviewService = Depends(get_live_interview_service),
    candidates: CandidateService = Depends(get_candidate_service),
    settings: AppSettings = Depends(get_app_settings),
    principal: Principal = Depends(require_authenticated),
) -> LiveSessionResponse:
    try:
        record = await live.get(session_id)
    except ConflictError as exc:
        raise NotFoundError("The live interview session was not found.") from exc
    await _enforce_ownership(record, principal, candidates, allow_recruiter=True)
    return _response(record, reconnect_after_seconds=settings.gemini_live_reconnect_seconds)


@router.post(
    "/live-sessions/{session_id}/token", response_model=LiveTokenResponse
)
async def issue_live_token(
    session_id: str,
    payload: LiveTokenRequest = LiveTokenRequest(),
    live: LiveInterviewService = Depends(get_live_interview_service),
    tokens: GeminiLiveTokenService = Depends(get_gemini_live_token_service),
    candidates: CandidateService = Depends(get_candidate_service),
    container: ServiceContainer = Depends(get_container),
    principal: Principal = Depends(require_authenticated),
) -> LiveTokenResponse:
    record = await live.get(session_id)
    await _enforce_ownership(record, principal, candidates, allow_recruiter=False)
    job_record = await container.job_repository.get(record.job_id)
    issued = await tokens.issue(
        session_record=record,
        recruiter_user_id=(
            job_record.created_by_user_id if job_record is not None else None
        ),
        resume=payload.resume,
    )
    return LiveTokenResponse(**issued.__dict__)


@router.post(
    "/live-sessions/{session_id}/finish", response_model=LiveFinishResponse
)
async def finish_live_session(
    session_id: str,
    payload: LiveFinishRequest | None = None,
    live: LiveInterviewService = Depends(get_live_interview_service),
    evaluations: EvaluationService = Depends(get_evaluation_service),
    candidates: CandidateService = Depends(get_candidate_service),
    principal: Principal = Depends(require_authenticated),
) -> LiveFinishResponse:
    existing = await live.get(session_id)
    await _enforce_ownership(existing, principal, candidates, allow_recruiter=False)
    if (
        payload
        and payload.pending_events
        and existing.live_state
        and existing.live_state.status in {LiveInterviewStatus.ACTIVE, LiveInterviewStatus.RECONNECTING}
    ):
        for event in sorted(payload.pending_events, key=lambda item: item.sequence):
            await live.append_event(session_id, event)
    record = await live.finish(
        session_id, reason="candidate_or_time_complete"
    )
    assert record.live_state is not None
    if record.application_id is None:
        return LiveFinishResponse(
            session_id=session_id,
            status=record.live_state.status,
            transcript_persisted=True,
        )
    evaluation = await evaluations.trigger_evaluation(session_id)
    return LiveFinishResponse(
        session_id=session_id,
        status=record.live_state.status,
        transcript_persisted=True,
        evaluation_id=evaluation.evaluation_id,
        evaluation_status=evaluation.status,
    )


async def _authenticate_control_socket(
    websocket: WebSocket,
    session_id: str,
    tokens: GeminiLiveTokenService,
) -> None:
    try:
        raw = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        message = ControlAuthenticate.model_validate(raw)
        tokens.verify_control_token(message.token, session_id)
    except (asyncio.TimeoutError, ValidationError, UnauthorizedError, ValueError):
        await websocket.send_json(
            {"type": "error", "code": "unauthorized", "detail": "Control authentication failed."}
        )
        await websocket.close(code=4401)
        raise


async def _complete_from_control(
    *,
    session_id: str,
    reason: str,
    live: LiveInterviewService,
    evaluations: EvaluationService,
) -> dict:
    record = await live.finish(session_id, reason=reason)
    assert record.live_state is not None
    if record.application_id is None:
        return {
            "type": "completed",
            "status": record.live_state.status.value,
            "evaluation_id": None,
            "evaluation_status": None,
        }
    evaluation = await evaluations.trigger_evaluation(session_id)
    return {
        "type": "completed",
        "status": record.live_state.status.value,
        "evaluation_id": evaluation.evaluation_id,
        "evaluation_status": evaluation.status.value,
    }


@router.websocket("/ws/live-sessions/{session_id}/control")
async def live_control_socket(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    live = build_live_interview_service(websocket.app)
    tokens = build_gemini_live_token_service(websocket.app)
    evaluations = build_evaluation_service(websocket.app)

    try:
        await _authenticate_control_socket(websocket, session_id, tokens)
        record = await live.get(session_id)
        state = record.live_state
        assert state is not None
        settings = build_app_settings(websocket.app)
        await websocket.send_json(
            {
                "type": "authenticated",
                "next_sequence": state.last_sequence + 1,
                "status": state.status.value,
                "wrap_up_at": state.wrap_up_at.isoformat() if state.wrap_up_at else None,
                "hard_stop_at": state.hard_stop_at.isoformat() if state.hard_stop_at else None,
                "has_resumption_handle": bool(state.resumption_handle),
                "reconnect_after_seconds": settings.gemini_live_reconnect_seconds,
            }
        )

        while True:
            raw = await websocket.receive_json()
            message_type = raw.get("type") if isinstance(raw, dict) else None
            model = CONTROL_MESSAGE_MODELS.get(message_type)
            if model is None or message_type == "authenticate":
                await websocket.send_json(
                    {"type": "error", "code": "unknown_message", "detail": "Unknown control message type."}
                )
                continue
            try:
                message = model.model_validate(raw)
                if isinstance(message, ControlTranscriptFinal):
                    record = await live.append_event(session_id, message.event)
                    assert record.live_state is not None
                    await websocket.send_json(
                        {"type": "event_ack", "sequence": message.event.sequence}
                    )
                elif isinstance(message, ControlProgress):
                    await live.record_progress(session_id, message.progress)
                    await websocket.send_json(
                        {"type": "tool_ack", "call_id": message.call_id}
                    )
                elif isinstance(message, ControlMetric):
                    await live.record_metric(session_id, message.metric)
                    await websocket.send_json({"type": "metric_ack"})
                elif isinstance(message, ControlResumptionHandle):
                    await live.store_resumption_handle(session_id, message.handle)
                    await websocket.send_json({"type": "resumption_handle_ack"})
                elif isinstance(message, ControlCompletionRequest):
                    decision = await live.completion_decision(session_id)
                    await websocket.send_json(
                        {
                            "type": "completion_decision",
                            "call_id": message.call_id,
                            **decision.model_dump(mode="json"),
                        }
                    )
                elif isinstance(message, ControlLifecycle):
                    if message.type == "started":
                        record = await live.start(session_id)
                        assert record.live_state is not None
                        await websocket.send_json(
                            {
                                "type": "started_ack",
                                "wrap_up_at": record.live_state.wrap_up_at.isoformat(),
                                "hard_stop_at": record.live_state.hard_stop_at.isoformat(),
                            }
                        )
                    elif message.type == "reconnecting":
                        await live.reconnecting(session_id)
                        await websocket.send_json({"type": "reconnecting_ack"})
                    elif message.type == "complete":
                        await websocket.send_json(
                            await _complete_from_control(
                                session_id=session_id,
                                reason=message.reason or "conversation_complete",
                                live=live,
                                evaluations=evaluations,
                            )
                        )
                    elif message.type == "failed":
                        await live.fail(
                            session_id,
                            category=message.reason or "client_transport_failed",
                        )
                        await websocket.send_json({"type": "failed_ack"})
            except ValidationError:
                await websocket.send_json(
                    {"type": "error", "code": "validation_error", "detail": "The control message was invalid."}
                )
            except ConflictError as exc:
                await websocket.send_json(
                    {"type": "error", "code": "conflict", "detail": exc.detail}
                )
    except WebSocketDisconnect:
        return
    except (asyncio.TimeoutError, ValidationError, UnauthorizedError, ValueError):
        return
    except NotFoundError:
        await websocket.send_json(
            {"type": "error", "code": "not_found", "detail": "The live interview session was not found."}
        )
        await websocket.close(code=4404)


__all__ = ["router"]
