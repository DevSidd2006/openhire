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

from api.errors import InvalidRequestError
from api.models import QuestionView
from api.registry import SessionNotFoundError, SessionRegistry
from utils.interview_session import InterviewSessionError
from utils.logging import get_logger
from utils.voice_turn import VoiceTurnError, VoiceTurnOutcome, VoiceTurnService

router = APIRouter(tags=["voice"])
logger = get_logger("api.voice")


def get_registry(request: Request) -> SessionRegistry:
    return request.app.state.registry


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


def _turn_payload(result, *, session_id: str) -> dict:
    """The single wire representation of a completed voice turn, shared by
    both transports."""
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
        "question_audio_base64": _encode_audio(result.question_audio),
        "audio_format": result.audio_format,
        "tts_error": result.tts_error,
        "status": result.session_status.value if result.session_status else None,
        "termination_reason": result.termination_reason,
        "latency_ms": {
            "stt": result.stt_latency_ms,
            "engine": result.engine_latency_ms,
            "tts": result.tts_latency_ms,
            "total": result.total_latency_ms,
        },
    }


# ---------------------------------------------------------------------------
# REST: one push-to-talk turn
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/voice-answers")
async def submit_voice_answer(
    session_id: str,
    payload: dict,
    request: Request,
    registry: SessionRegistry = Depends(get_registry),
) -> dict:
    """POST /sessions/{id}/voice-answers - submit one spoken answer.

    Body: {"audio_base64": "...", "audio_format": "wav"}. The audio is
    transcribed and passed to InterviewSessionRunner.submit_answer()
    exactly as a typed answer would be; this handler never inspects or
    interprets either the audio or the resulting text.
    """
    runner = await registry.get_session(session_id)
    request.state.runner = runner

    raw = payload.get("audio_base64")
    if not isinstance(raw, str):
        raise InvalidRequestError("audio_base64 (string) is required")
    try:
        audio = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise InvalidRequestError("audio_base64 is not valid base64") from exc

    service = build_voice_service(request.app)
    result = await service.submit_audio_answer(
        runner,
        audio,
        audio_format=payload.get("audio_format", "wav"),
        utterance_id=payload.get("utterance_id"),
    )

    logger.info(
        f"voice turn: session_id={session_id} outcome={result.outcome.value} "
        f"transcript_chars={len(result.transcript)}"
    )
    return _turn_payload(result, session_id=session_id)


@router.get("/sessions/{session_id}/current-question-audio")
async def current_question_audio(
    session_id: str,
    request: Request,
    registry: SessionRegistry = Depends(get_registry),
) -> dict:
    """Render the CURRENT pending question as audio without submitting
    anything - used to voice the first question, and to re-play the pending
    question after a reconnect. Touches no interview state."""
    runner = await registry.get_session(session_id)
    request.state.runner = runner

    service = build_voice_service(request.app)
    result = await service.speak_current_question(runner)
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
    registry: SessionRegistry = websocket.app.state.registry
    await websocket.accept()

    try:
        runner = await registry.get_session(session_id)
    except SessionNotFoundError:
        await websocket.send_json({"type": "error", "error": "session_not_found"})
        await websocket.close(code=4404)
        return

    service = build_voice_service(websocket.app)

    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")

            if kind == "speak_question":
                try:
                    result = await service.speak_current_question(runner)
                except VoiceTurnError as exc:
                    await websocket.send_json(
                        {"type": "error", "error": "voice_error", "detail": str(exc)}
                    )
                    continue
                await websocket.send_json(_turn_payload(result, session_id=session_id))
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
                    result = await service.submit_audio_answer(
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

                await websocket.send_json(_turn_payload(result, session_id=session_id))

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
