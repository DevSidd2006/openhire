"""
P9: voice transport (WebSocket + REST).

Both transports below are THIN. Neither contains interview intelligence,
speech logic, or state: each one decodes a payload, calls exactly one
utils/voice_turn.VoiceTurnService method, and shapes a response. The
interview engine (InterviewSessionRunner) and the voice orchestration
(VoiceTurnService) are unchanged by, and unaware of, which transport
invoked them.

Why a WebSocket at all: a live interview is a long-lived, bidirectional,
turn-taking conversation. Over REST the client must poll to discover that
the interviewer has finished thinking, and every turn re-establishes a
connection while carrying audio in both directions. A single socket per
session matches the interaction shape directly, and is the prerequisite for
the streaming/partial-transcript/barge-in work in P9 Phase 8. The REST
endpoint is kept alongside it because it is trivially testable, works from
`curl`, and is sufficient for push-to-talk clients - they share one
implementation, so neither can drift.

Session ownership: both transports resolve the session ONLY by the opaque
session_id issued by api/registry.py (uuid4, 122 bits). candidate_id/job_id
are never accepted as a lookup key, so one candidate's identifier can never
be used to reach another candidate's session (P5 Phase 9 / P9 Phase 9).
"""
import base64
from typing import Optional

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from api.errors import InvalidRequestError
from api.models import QuestionView
from api.registry import SessionNotFoundError
from core.dependencies import (
    build_evaluation_service,
    build_interview_service,
    get_evaluation_service,
    get_interview_service,
)
from core.logging import get_logger
from core.security import Principal, require_authenticated
from repositories.interfaces import EvaluationJob
from services.evaluation_service import EvaluationService
from services.interview_service import InterviewService
from utils.interview_session import InterviewSessionError, SessionStatus
from utils.voice_turn import VoiceTurnError, VoiceTurnService

router = APIRouter(tags=["voice"])
logger = get_logger("api.voice")


def build_voice_service(app) -> VoiceTurnService:
    """Resolve the voice service for this app.

    `app.state.voice_service_factory` is the same kind of pure wiring seam
    `interviewer_factory` already is (see api/app.py) - it lets a test
    inject deterministic mock speech providers without any monkeypatching
    of the provider factories, and is None in a real deployment.
    """
    factory = getattr(app.state, "voice_service_factory", None)
    if factory is not None:
        return factory()

    from providers.audio import get_audio_processor, get_speech_synthesizer

    return VoiceTurnService(stt=get_audio_processor(), tts=get_speech_synthesizer())


def _encode_audio(audio: Optional[bytes]) -> Optional[str]:
    """Audio crosses JSON as base64. Kept in one helper so the REST and
    WebSocket representations can never diverge."""
    if audio is None:
        return None
    return base64.b64encode(audio).decode("ascii")


class VoiceAnswerRequest(BaseModel):
    """Body of POST /sessions/{id}/voice-answers.

    Previously typed as a bare `dict`, so the endpoint had no schema, no
    generated OpenAPI documentation, and validated its own fields by hand.
    Declaring it means an oversized or wrong-typed payload is rejected by
    FastAPI before any handler code runs - and `max_length` bounds the
    base64 string at the transport edge, ahead of the domain-level
    MAX_UTTERANCE_BYTES check in utils/voice_turn.py (which still applies
    and is still the authority on decoded audio size).

    `extra="forbid"` so a client typo such as `audio_b64` fails loudly
    instead of silently sending no audio at all.
    """

    model_config = ConfigDict(extra="forbid")

    # 16 MiB of base64 ~= 12 MiB of audio, matching the default
    # MAX_REQUEST_BODY_BYTES ceiling enforced by the middleware.
    audio_base64: str = Field(min_length=1, max_length=16 * 1024 * 1024)
    audio_format: str = Field(default="wav", max_length=16)
    utterance_id: Optional[str] = Field(default=None, max_length=128)


def _decode_audio(raw: str) -> bytes:
    """Decode a base64 audio payload, or raise a 400.

    `validate=True` so that stray non-base64 characters are an error rather
    than being silently discarded - silent discarding would hand the STT
    provider a corrupted buffer and turn a client bug into an unexplained
    transcription failure.
    """
    try:
        return base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise InvalidRequestError("audio_base64 is not valid base64") from exc


def _turn_payload(
    result, *, session_id: str, evaluation_job: Optional[EvaluationJob] = None,
) -> dict:
    """The single wire representation of a completed voice turn, shared by
    both transports.

    `evaluation_job` (Chunk 4) is additive: omitted (the default), the
    dict is byte-for-byte what it always was - both the `speak_question`
    reply (which never advances the interview) and any pre-Chunk-4 caller
    are unaffected.
    """
    return {
        "type": "turn_result",
        "session_id": session_id,
        "outcome": result.outcome.value,
        "transcript": result.transcript,
        "next_question": (
            QuestionView.from_domain(result.submission.next_question).model_dump()
            if result.submission is not None and result.submission.next_question is not None
            else None
        ),
        # `speak_question`'s reply (the very first question, and any
        # reconnect replay) never advances the interview, so `submission`
        # is always None and `next_question` above is always null - but the
        # question text itself is not: VoiceTurnResult.next_question_text
        # carries it (utils/voice_turn.py:speak_current_question). Without
        # this field on the wire, a client relying solely on `next_question`
        # never learns the question's text at all, only its audio.
        "next_question_text": result.next_question_text,
        "question_audio_base64": _encode_audio(result.question_audio),
        "audio_format": result.audio_format,
        "tts_error": result.tts_error,
        "status": result.session_status.value if result.session_status else None,
        "termination_reason": result.termination_reason,
        "evaluation_id": evaluation_job.evaluation_id if evaluation_job else None,
        "evaluation_status": evaluation_job.status.value if evaluation_job else None,
        "latency_ms": {
            "stt": result.stt_latency_ms,
            "engine": result.engine_latency_ms,
            "tts": result.tts_latency_ms,
            "total": result.total_latency_ms,
        },
    }


async def _trigger_evaluation_after_voice_turn(
    evaluations: EvaluationService, service: InterviewService,
    *, session_id: str, runner,
) -> Optional[EvaluationJob]:
    """The voice-transport equivalent of
    api/routes/interview.py:_trigger_evaluation_if_ready - same policy
    (only once the transcript is CONFIRMED persisted), duplicated rather
    than imported across route modules to keep each transport's route
    module self-contained, matching how this file already keeps its own
    `_turn_payload`/`_encode_audio` rather than sharing api/routes/interview.py's."""
    if runner.status != SessionStatus.SEALED:
        return None
    transcript_persisted = await service.get_transcript_persistence_status(runner)
    if not transcript_persisted:
        return None
    return await evaluations.trigger_evaluation(session_id)


# ---------------------------------------------------------------------------
# REST: one push-to-talk turn
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/voice-answers")
async def submit_voice_answer(
    session_id: str,
    payload: VoiceAnswerRequest,
    request: Request,
    service: InterviewService = Depends(get_interview_service),
    evaluations: EvaluationService = Depends(get_evaluation_service),
    principal: Principal = Depends(require_authenticated),
) -> dict:
    """POST /sessions/{id}/voice-answers - submit one spoken answer.

    Body: {"audio_base64": "...", "audio_format": "wav"}. The audio is
    transcribed and passed to InterviewSessionRunner.submit_answer()
    exactly as a typed answer would be; this handler never inspects or
    interprets either the audio or the resulting text.
    """
    runner = await service.get_runner(session_id)
    request.state.runner = runner

    audio = _decode_audio(payload.audio_base64)

    voice = build_voice_service(request.app)
    result = await voice.submit_audio_answer(
        runner,
        audio,
        audio_format=payload.audio_format,
        utterance_id=payload.utterance_id,
    )

    # A spoken turn advances the same runner a typed turn does, so it must
    # produce the same durable record. VoiceTurnService drives the runner
    # itself (see services/interview_service.record_turn_outcome), so the
    # persistence write is made here rather than inside it.
    await service.record_turn_outcome(session_id, runner)

    # Chunk 4: same trigger point as the typed-answer route
    # (api/routes/interview.py) - only once sealed AND the transcript is
    # confirmed persisted, scheduled in the background, never awaited here.
    evaluation_job = await _trigger_evaluation_after_voice_turn(
        evaluations, service, session_id=session_id, runner=runner,
    )

    logger.info(
        "voice turn complete",
        extra={
            "event": "voice_turn",
            "session_id": session_id,
            "outcome": result.outcome.value,
            # Length only - the transcript is candidate speech and is never
            # written to a log.
            "transcript_chars": len(result.transcript),
        },
    )
    return _turn_payload(result, session_id=session_id, evaluation_job=evaluation_job)


@router.get("/sessions/{session_id}/current-question-audio")
async def current_question_audio(
    session_id: str,
    request: Request,
    service: InterviewService = Depends(get_interview_service),
    principal: Principal = Depends(require_authenticated),
) -> dict:
    """Render the CURRENT pending question as audio without submitting
    anything - used to voice the first question, and to re-play the pending
    question after a reconnect. Touches no interview state."""
    runner = await service.get_runner(session_id)
    request.state.runner = runner

    voice = build_voice_service(request.app)
    result = await voice.speak_current_question(runner)
    return {
        "session_id": session_id,
        "question_text": result.next_question_text,
        "question_audio_base64": _encode_audio(result.question_audio),
        "audio_format": result.audio_format,
    }


# ---------------------------------------------------------------------------
# WebSocket: live turn-taking
# ---------------------------------------------------------------------------

@router.websocket("/ws/sessions/{session_id}")
async def voice_socket(websocket: WebSocket, session_id: str) -> None:
    """One socket per interview session.

    Client -> server messages (JSON):
      {"type": "speak_question"}                      -> re-send current question audio
      {"type": "answer", "audio_base64": "...",
       "audio_format": "wav"}                         -> one full voice turn

    Server -> client messages: `_turn_payload(...)` (type="turn_result"),
    or {"type": "error", ...}.

    Disconnect safety (P9 Phase 6): this handler holds NO session state.
    The runner lives in the registry and every state transition happens
    inside InterviewSessionRunner under its own asyncio lock, so a dropped
    connection mid-turn cannot corrupt the session - at worst an in-flight
    turn completes and its result is never delivered, and the runner's
    per-question idempotency cache means re-sending that same answer after
    reconnecting returns the original result instead of double-recording it.
    """
    service = build_interview_service(websocket.app)
    evaluations = build_evaluation_service(websocket.app)
    await websocket.accept()

    try:
        runner = await service.get_runner(session_id)
    except SessionNotFoundError:
        await websocket.send_json({"type": "error", "error": "session_not_found"})
        await websocket.close(code=4404)
        return

    voice = build_voice_service(websocket.app)

    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")

            if kind == "speak_question":
                try:
                    result = await voice.speak_current_question(runner)
                except VoiceTurnError as exc:
                    await websocket.send_json(
                        {"type": "error", "error": "voice_error", "detail": str(exc)}
                    )
                    continue
                await websocket.send_json(_turn_payload(result, session_id=session_id))
                continue

            if kind == "answer_text":
                transcript_text = message.get("transcript")
                if not isinstance(transcript_text, str):
                    await websocket.send_json(
                        {"type": "error", "error": "invalid_request",
                         "detail": "transcript (string) is required for answer_text"}
                    )
                    continue

                try:
                    result = await voice.submit_text_answer(
                        runner,
                        transcript_text,
                        utterance_id=message.get("utterance_id"),
                    )
                except VoiceTurnError as exc:
                    await websocket.send_json(
                        {"type": "error", "error": "voice_error", "detail": str(exc)}
                    )
                    continue
                except InterviewSessionError as exc:
                    await websocket.send_json(
                        {"type": "error", "error": "session_error", "detail": str(exc)}
                    )
                    continue

                await service.record_turn_outcome(session_id, runner)
                evaluation_job = await _trigger_evaluation_after_voice_turn(
                    evaluations, service, session_id=session_id, runner=runner,
                )

                await websocket.send_json(
                    _turn_payload(result, session_id=session_id, evaluation_job=evaluation_job)
                )

                if result.session_status is not None and result.session_status.value == "sealed":
                    await websocket.close(code=1000)
                    return
                continue

            if kind == "answer":
                raw = message.get("audio_base64")
                if not isinstance(raw, str):
                    await websocket.send_json(
                        {"type": "error", "error": "invalid_request",
                         "detail": "audio_base64 (string) is required"}
                    )
                    continue
                try:
                    audio = base64.b64decode(raw, validate=True)
                except Exception:
                    await websocket.send_json(
                        {"type": "error", "error": "invalid_request",
                         "detail": "audio_base64 is not valid base64"}
                    )
                    continue

                try:
                    result = await voice.submit_audio_answer(
                        runner,
                        audio,
                        audio_format=message.get("audio_format", "wav"),
                        utterance_id=message.get("utterance_id"),
                    )
                except VoiceTurnError as exc:
                    # STT failed - the pending question is untouched, so the
                    # candidate can simply speak again.
                    await websocket.send_json(
                        {"type": "error", "error": "speech_error", "detail": str(exc)}
                    )
                    continue
                except InterviewSessionError as exc:
                    await websocket.send_json(
                        {"type": "error", "error": "session_error", "detail": str(exc)}
                    )
                    continue

                # Same durable record a typed turn produces (see the REST
                # handler above). Persistence failures are logged inside the
                # service and never break the live socket.
                await service.record_turn_outcome(session_id, runner)

                # Chunk 4: same trigger point as the REST voice handler.
                evaluation_job = await _trigger_evaluation_after_voice_turn(
                    evaluations, service, session_id=session_id, runner=runner,
                )

                await websocket.send_json(
                    _turn_payload(result, session_id=session_id, evaluation_job=evaluation_job)
                )

                if result.session_status is not None and result.session_status.value == "sealed":
                    await websocket.close(code=1000)
                    return
                continue

            await websocket.send_json(
                {"type": "error", "error": "unknown_message_type", "detail": str(kind)}
            )

    except WebSocketDisconnect:
        # Normal client disconnect - nothing to clean up, because this
        # handler owns no session state (see the docstring).
        logger.info(f"voice socket disconnected: session_id={session_id}")
        return
