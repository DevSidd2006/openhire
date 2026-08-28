"""
P9: voice turn orchestration.

The ONE place the voice path is wired together:

    candidate audio bytes
      -> AudioProcessor.transcribe()          (speech to text)
      -> InterviewSessionRunner.submit_answer()  (THE interview engine)
      -> SpeechSynthesizer.synthesize()       (text to speech)
      -> interviewer audio bytes

Strict separation of responsibilities (identical in spirit to
utils/interview_session.py's own contract, which this module sits on top
of and never bypasses):

    VoiceTurnService (this file) - transcode + sequencing ONLY. It never
        decides what to ask, never scores an answer, never builds evidence,
        never decides whether the interview should end, and never touches
        InterviewState. Every one of those decisions stays where it already
        lives (utils/adaptive_interview.py, utils/interview_session.py,
        agents/interviewer/agent.py).
    InterviewSessionRunner - remains the single source of truth for
        interview state, evidence, transcript sealing, idempotency, and
        concurrency. This module calls exactly one of its methods per turn.
    AudioProcessor / SpeechSynthesizer - the swappable speech providers.

Because the runner is untouched, every guarantee it already carries -
answer immutability, question immutability, per-question idempotency, the
asyncio lock, candidate/job isolation, "never fabricate an evaluation",
sealed-transcript invariants - applies to a voice interview exactly as it
does to a typed one. A voice turn is a typed turn with two transcoding
steps bolted on either side; it is deliberately NOT a second interview
engine.

Failure policy (P9 Phase 6), explicit and never silent:
  * STT provider failure -> VoiceTurnError. No answer is submitted. The
    pending question stays pending, so the candidate can simply speak
    again - the session is not failed or corrupted by a transcription
    outage.
  * Empty / whitespace / below-MIN_TRANSCRIPT_CHARS transcription -> a
    NO_SPEECH result. This is NOT an error and NOT an answer: silence, a
    cough, or a dropped mic must never be recorded as the candidate's
    response to a question (which would be scored, evidenced, and sealed
    into the transcript as if they had actually said it).
  * TTS provider failure -> the answer IS still recorded (the runner
    already committed it and the transcript is authoritative), but the
    result carries question_audio=None plus an explicit tts_error. Losing
    the audio rendering of a question must never roll back or duplicate a
    successfully recorded answer.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Dict, Optional

from pydantic import BaseModel, ConfigDict

from config.settings import MAX_UTTERANCE_BYTES, MIN_TRANSCRIPT_CHARS
from providers.base import AudioProcessor, SpeechError, SpeechSynthesizer
from utils.interview_session import (
    AnswerSubmissionResult,
    InterviewSessionRunner,
    SessionStatus,
)
from utils.logging import get_logger

logger = get_logger("voice.turn")


class VoiceTurnError(Exception):
    """A voice turn could not be completed. Raised instead of silently
    submitting an empty/garbage answer or fabricating a transcription -
    same "explicit failure over fabricated success" policy the rest of the
    codebase follows (P0/P4/P9)."""


class VoiceTurnOutcome(str, Enum):
    """Why a voice turn ended the way it did - lets a transport/client
    distinguish "you were not heard, try again" from "answer accepted"
    without string-matching a message."""

    ANSWER_RECORDED = "answer_recorded"
    NO_SPEECH_DETECTED = "no_speech_detected"


class VoiceTurnResult(BaseModel):
    """What one voice turn produced.

    `transcript` is the candidate's speech as transcribed, stored verbatim
    - it is the exact string handed to InterviewSessionRunner, so what the
    interview scored and what the candidate is shown can never diverge.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    outcome: VoiceTurnOutcome
    transcript: str = ""
    submission: Optional[AnswerSubmissionResult] = None
    next_question_text: Optional[str] = None
    question_audio: Optional[bytes] = None
    audio_format: str = "wav"
    tts_error: Optional[str] = None
    session_status: Optional[SessionStatus] = None
    termination_reason: Optional[str] = None
    stt_latency_ms: Optional[float] = None
    engine_latency_ms: Optional[float] = None
    tts_latency_ms: Optional[float] = None

    @property
    def total_latency_ms(self) -> Optional[float]:
        parts = [self.stt_latency_ms, self.engine_latency_ms, self.tts_latency_ms]
        present = [p for p in parts if p is not None]
        return sum(present) if present else None


class VoiceTurnService:
    """Drives voice turns for ONE interview session.

    Holds no state of its own beyond its two providers - the session's
    state lives entirely in the InterviewSessionRunner passed to each call,
    so this service is safe to share across sessions and can never leak one
    candidate's turn into another's session (the runner is always supplied
    by the caller, looked up by session_id in api/registry.py).
    """

    def __init__(
        self,
        stt: AudioProcessor,
        tts: SpeechSynthesizer,
        *,
        voice: Optional[str] = None,
    ):
        self.stt = stt
        self.tts = tts
        self.voice = voice
        # P9: utterance-level replay guard, keyed by
        # (interview_id, utterance_id). InterviewSessionRunner already
        # guarantees ONE pending question can never be answered twice (its
        # per-question idempotency cache, P4 Phase 11) - but that key is the
        # QUESTION, so it cannot help when a replayed utterance arrives
        # after the question has already advanced: that submission
        # legitimately targets a different question, and identical text for
        # two different questions is not inherently wrong (a candidate may
        # genuinely repeat themselves). Only the CLIENT knows "this is the
        # same recording I already sent", so it supplies an utterance_id and
        # this cache honors it. Bounded per process lifetime and never used
        # when utterance_id is omitted, so existing behavior is unchanged.
        self._seen_utterances: Dict[tuple, "VoiceTurnResult"] = {}

    # -- speech in ------------------------------------------------------

    async def transcribe(self, audio: bytes, audio_format: str = "wav") -> str:
        """Audio -> text, with explicit failure. Never returns a fabricated
        or default transcription when the provider fails."""
        if not isinstance(audio, (bytes, bytearray)):
            raise VoiceTurnError("Audio payload must be raw bytes")
        if len(audio) == 0:
            # Not a provider failure - genuinely nothing was captured.
            return ""
        if len(audio) > MAX_UTTERANCE_BYTES:
            # Bounded input: an unbounded upload is both a cost and a
            # denial-of-service concern on a public endpoint (P9 Phase 9).
            raise VoiceTurnError(
                f"Utterance exceeds the maximum allowed size of {MAX_UTTERANCE_BYTES} bytes"
            )

        try:
            text = await self.stt.transcribe(bytes(audio), format=audio_format)
        except SpeechError as exc:
            # Deliberately does not include the provider's raw message in
            # anything client-facing - api/errors.py owns that boundary.
            logger.warning(f"speech-to-text failed: {type(exc).__name__}")
            raise VoiceTurnError(f"Speech-to-text failed: {exc}") from exc
        except Exception as exc:
            logger.error(f"speech-to-text raised an unexpected error: {type(exc).__name__}")
            raise VoiceTurnError(f"Speech-to-text failed: {exc}") from exc

        return (text or "").strip()

    # -- speech out -----------------------------------------------------

    async def speak(self, text: str) -> bytes:
        """Text -> audio, with explicit failure. The text is passed through
        EXACTLY as given (see SpeechSynthesizer.synthesize's contract)."""
        try:
            return await self.tts.synthesize(text, voice=self.voice)
        except SpeechError as exc:
            logger.warning(f"text-to-speech failed: {type(exc).__name__}")
            raise VoiceTurnError(f"Text-to-speech failed: {exc}") from exc
        except Exception as exc:
            logger.error(f"text-to-speech raised an unexpected error: {type(exc).__name__}")
            raise VoiceTurnError(f"Text-to-speech failed: {exc}") from exc

    async def speak_current_question(self, runner: InterviewSessionRunner) -> VoiceTurnResult:
        """Render the session's CURRENT pending question as audio, without
        submitting anything. Used to voice the very first question after
        start() (there is no answer yet), and to re-play the pending
        question on reconnect - neither of which is a turn, so neither may
        touch interview state."""
        question = runner.get_current_question()
        if question is None:
            raise VoiceTurnError("No question is currently pending")

        started = time.monotonic()
        audio = await self.speak(question.question_text)
        tts_ms = (time.monotonic() - started) * 1000.0

        return VoiceTurnResult(
            outcome=VoiceTurnOutcome.ANSWER_RECORDED,
            next_question_text=question.question_text,
            question_audio=audio,
            audio_format=getattr(self.tts, "audio_format", "wav"),
            session_status=runner.status,
            tts_latency_ms=tts_ms,
        )

    # -- one full turn --------------------------------------------------

    async def submit_audio_answer(
        self,
        runner: InterviewSessionRunner,
        audio: bytes,
        audio_format: str = "wav",
        utterance_id: Optional[str] = None,
    ) -> VoiceTurnResult:
        """One complete voice turn. See this module's docstring for the
        full flow and failure policy.

        Note what this method does NOT do: it does not inspect the
        transcript's content, judge it, or decide what happens next. The
        only judgement it makes is the transport-level one of "was anything
        actually said?" - everything after that is
        InterviewSessionRunner.submit_answer()'s decision.

        `utterance_id` (optional) is a client-generated id for ONE physical
        recording. Re-sending the same utterance_id returns the original
        result instead of submitting the audio again - see the
        `_seen_utterances` comment in __init__ for why the runner's own
        per-question idempotency cannot cover this case. Omitting it
        preserves the previous behavior exactly.
        """
        replay_key = (runner.get_state().interview_id, utterance_id) if utterance_id else None
        if replay_key is not None:
            cached = self._seen_utterances.get(replay_key)
            if cached is not None:
                logger.info("replayed utterance ignored - returning the original turn result")
                return cached

        stt_started = time.monotonic()
        transcript = await self.transcribe(audio, audio_format=audio_format)
        stt_ms = (time.monotonic() - stt_started) * 1000.0

        if len(transcript) < MIN_TRANSCRIPT_CHARS:
            # NOT an answer and NOT an error. The pending question is
            # untouched, so the candidate can simply speak again - nothing
            # was recorded, scored, evidenced, or sealed (P9 Phase 6:
            # "empty transcription does not silently become an answer").
            logger.info("no speech detected in utterance - no answer submitted")
            # Deliberately NOT cached under replay_key: nothing was
            # recorded, so re-sending after a genuine retry should get a
            # fresh attempt rather than a sticky "no speech" verdict.
            return VoiceTurnResult(
                outcome=VoiceTurnOutcome.NO_SPEECH_DETECTED,
                transcript=transcript,
                session_status=runner.status,
                stt_latency_ms=stt_ms,
            )

        engine_started = time.monotonic()
        # THE interview engine. Everything that matters - evaluation,
        # evidence, state transition, termination, sealing, idempotency,
        # concurrency - happens inside this single call, unchanged by P9.
        submission = await runner.submit_answer(transcript)
        engine_ms = (time.monotonic() - engine_started) * 1000.0

        next_question = submission.next_question
        question_audio: Optional[bytes] = None
        tts_error: Optional[str] = None
        tts_ms: Optional[float] = None

        if next_question is not None:
            tts_started = time.monotonic()
            try:
                question_audio = await self.speak(next_question.question_text)
            except VoiceTurnError as exc:
                # The answer is already committed to the session and the
                # transcript. Failing the whole turn here would strand the
                # candidate: the runner's per-question idempotency cache
                # would return this same submission on a retry, so the
                # answer can never be double-recorded, but the caller still
                # needs to know the audio is missing.
                tts_error = str(exc)
                logger.warning("question audio unavailable for this turn (TTS failed)")
            tts_ms = (time.monotonic() - tts_started) * 1000.0

        result = VoiceTurnResult(
            outcome=VoiceTurnOutcome.ANSWER_RECORDED,
            transcript=transcript,
            submission=submission,
            next_question_text=next_question.question_text if next_question else None,
            question_audio=question_audio,
            audio_format=getattr(self.tts, "audio_format", "wav"),
            tts_error=tts_error,
            session_status=submission.session_status,
            termination_reason=submission.termination_reason,
            stt_latency_ms=stt_ms,
            engine_latency_ms=engine_ms,
            tts_latency_ms=tts_ms,
        )
        if replay_key is not None:
            self._seen_utterances[replay_key] = result
        return result

    async def submit_text_answer(
        self,
        runner: InterviewSessionRunner,
        transcript: str,
        utterance_id: Optional[str] = None,
    ) -> VoiceTurnResult:
        """Submit a pre-transcribed text answer (e.g. from browser-native STT).
        Advances the runner and synthesizes audio for the next question via TTS."""
        replay_key = (runner.get_state().interview_id, utterance_id) if utterance_id else None
        if replay_key is not None:
            cached = self._seen_utterances.get(replay_key)
            if cached is not None:
                logger.info("replayed utterance ignored - returning the original turn result")
                return cached

        clean_transcript = (transcript or "").strip()
        if len(clean_transcript) < MIN_TRANSCRIPT_CHARS:
            logger.info("no speech / empty transcript provided - no answer submitted")
            return VoiceTurnResult(
                outcome=VoiceTurnOutcome.NO_SPEECH_DETECTED,
                transcript=clean_transcript,
                session_status=runner.status,
                stt_latency_ms=0.0,
            )

        engine_started = time.monotonic()
        submission = await runner.submit_answer(clean_transcript)
        engine_ms = (time.monotonic() - engine_started) * 1000.0

        next_question = submission.next_question
        question_audio: Optional[bytes] = None
        tts_error: Optional[str] = None
        tts_ms: Optional[float] = None

        if next_question is not None:
            tts_started = time.monotonic()
            try:
                question_audio = await self.speak(next_question.question_text)
            except VoiceTurnError as exc:
                tts_error = str(exc)
                logger.warning("question audio unavailable for this turn (TTS failed)")
            tts_ms = (time.monotonic() - tts_started) * 1000.0

        result = VoiceTurnResult(
            outcome=VoiceTurnOutcome.ANSWER_RECORDED,
            transcript=clean_transcript,
            submission=submission,
            next_question_text=next_question.question_text if next_question else None,
            question_audio=question_audio,
            audio_format=getattr(self.tts, "audio_format", "wav"),
            tts_error=tts_error,
            session_status=submission.session_status,
            termination_reason=submission.termination_reason,
            stt_latency_ms=0.0,
            engine_latency_ms=engine_ms,
            tts_latency_ms=tts_ms,
        )
        if replay_key is not None:
            self._seen_utterances[replay_key] = result
        return result

