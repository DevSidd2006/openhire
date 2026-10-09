"""Application service for durable Gemini Live interview state."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from core.errors import ConflictError, NotFoundError
from repositories.interfaces import SessionRecord, SessionRepository, TranscriptRepository
from schemas.job import JobDescription
from schemas.live_interview import (
    CompetencyProgress,
    CompletionDecision,
    LiveInterviewState,
    LiveInterviewStatus,
    LiveLatencyMetric,
    LiveTranscriptEvent,
)
from schemas.resume import ParsedResume
from schemas.rubric import JobRubric
from services.live_transcript import project_live_transcript
from utils.interview_session import SessionStatus


_STATUS_MAP = {
    LiveInterviewStatus.CREATED: SessionStatus.CREATED,
    LiveInterviewStatus.CONNECTING: SessionStatus.ACTIVE,
    LiveInterviewStatus.ACTIVE: SessionStatus.ACTIVE,
    LiveInterviewStatus.RECONNECTING: SessionStatus.ACTIVE,
    LiveInterviewStatus.FINISHING: SessionStatus.FINISHING,
    LiveInterviewStatus.SEALED: SessionStatus.SEALED,
    LiveInterviewStatus.FAILED: SessionStatus.FAILED,
}

_TERMINAL = {LiveInterviewStatus.SEALED, LiveInterviewStatus.FAILED}
_ACTIVE = {LiveInterviewStatus.ACTIVE, LiveInterviewStatus.RECONNECTING}


class LiveInterviewService:
    """Own lifecycle transitions for one append-only realtime session.

    A lock per session keeps concurrent WebSocket messages ordered within a
    process.  The state itself remains the durable source of truth so a
    restart can continue ingesting events or retry transcript sealing.
    """

    def __init__(
        self,
        session_repository: SessionRepository,
        transcript_repository: TranscriptRepository,
        *,
        wrap_up_seconds: int = 12 * 60,
        hard_stop_seconds: int = 15 * 60,
        clock_skew_seconds: int = 5,
        now: Callable[[], datetime] | None = None,
        locks: dict[str, asyncio.Lock] | None = None,
        locks_guard: asyncio.Lock | None = None,
    ) -> None:
        self._sessions = session_repository
        self._transcripts = transcript_repository
        self._wrap_up_seconds = wrap_up_seconds
        self._hard_stop_seconds = hard_stop_seconds
        self._clock_skew_ms = clock_skew_seconds * 1000
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._locks: dict[str, asyncio.Lock] = locks if locks is not None else {}
        self._locks_guard = locks_guard if locks_guard is not None else asyncio.Lock()

    async def _lock_for(self, session_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(session_id, asyncio.Lock())

    async def create_session(
        self,
        *,
        job_description: JobDescription,
        parsed_resume: ParsedResume,
        rubric_snapshot: JobRubric,
        candidate_id: str,
        application_id: str | None = None,
    ) -> SessionRecord:
        if parsed_resume.candidate_id != candidate_id:
            raise ConflictError("The resume does not belong to this candidate.")
        if rubric_snapshot.job_id != job_description.job_id:
            raise ConflictError("The rubric does not belong to this job.")

        session_id = f"sess_{uuid4().hex}"
        interview_id = f"int_{uuid4().hex}"
        state = LiveInterviewState(
            interview_id=interview_id,
            rubric_snapshot=rubric_snapshot.model_copy(deep=True),
        )
        record = SessionRecord(
            session_id=session_id,
            interview_id=interview_id,
            candidate_id=candidate_id,
            job_id=job_description.job_id,
            application_id=application_id,
            status=SessionStatus.CREATED,
            job_description=job_description.model_copy(deep=True),
            parsed_resume=parsed_resume.model_copy(deep=True),
            interview_mode="gemini_live",
            live_state=state,
        )
        return await self._sessions.save(record)

    async def get(self, session_id: str) -> SessionRecord:
        return await self._get_live(session_id)

    async def start(self, session_id: str) -> SessionRecord:
        lock = await self._lock_for(session_id)
        async with lock:
            record = await self._get_live(session_id)
            state = record.live_state
            assert state is not None

            if state.status == LiveInterviewStatus.ACTIVE:
                return record
            if state.status in _TERMINAL or state.status == LiveInterviewStatus.FINISHING:
                raise self._state_conflict(record, "start")

            now = self._utc_now()
            if state.status == LiveInterviewStatus.RECONNECTING:
                updated = state.model_copy(
                    update={
                        "status": LiveInterviewStatus.ACTIVE,
                        "last_activity_at": now,
                    }
                )
            elif state.status in {
                LiveInterviewStatus.CREATED,
                LiveInterviewStatus.CONNECTING,
            }:
                started_at = state.started_at or now
                updated = state.model_copy(
                    update={
                        "status": LiveInterviewStatus.ACTIVE,
                        "started_at": started_at,
                        "last_activity_at": now,
                        "wrap_up_at": started_at
                        + timedelta(seconds=self._wrap_up_seconds),
                        "hard_stop_at": started_at
                        + timedelta(seconds=self._hard_stop_seconds),
                    }
                )
            else:
                raise self._state_conflict(record, "start")
            return await self._save_live_state(record, updated)

    async def reconnecting(self, session_id: str) -> SessionRecord:
        lock = await self._lock_for(session_id)
        async with lock:
            record = await self._get_live(session_id)
            state = self._require_active(record, "reconnect")
            if state.status == LiveInterviewStatus.RECONNECTING:
                return record
            updated = state.model_copy(
                update={
                    "status": LiveInterviewStatus.RECONNECTING,
                    "last_activity_at": self._utc_now(),
                    "reconnect_count": state.reconnect_count + 1,
                }
            )
            return await self._save_live_state(record, updated)

    async def append_event(
        self, session_id: str, event: LiveTranscriptEvent
    ) -> SessionRecord:
        lock = await self._lock_for(session_id)
        async with lock:
            record = await self._get_live(session_id)
            state = self._require_active(record, "append a transcript event")

            for accepted in state.events:
                if accepted.event_id == event.event_id:
                    if accepted == event:
                        return record
                    raise ConflictError(
                        "A transcript event ID was reused with different content."
                    )

            expected = state.last_sequence + 1
            if event.sequence != expected:
                raise ConflictError(
                    f"Transcript sequence must be {expected}; received {event.sequence}."
                )
            if event.started_at_ms > event.ended_at_ms:
                raise ConflictError("Transcript event timestamps are reversed.")
            if state.events:
                previous = state.events[-1]
                if (
                    event.started_at_ms < previous.started_at_ms
                    or event.ended_at_ms < previous.ended_at_ms
                ):
                    raise ConflictError("Transcript event timestamps moved backward.")
            if state.started_at is None or state.hard_stop_at is None:
                raise self._state_conflict(record, "append a transcript event")
            hard_stop_elapsed_ms = int(
                (state.hard_stop_at - state.started_at).total_seconds() * 1000
            )
            if event.ended_at_ms > hard_stop_elapsed_ms + self._clock_skew_ms:
                raise ConflictError("Transcript event is beyond the interview hard stop.")

            updated = state.model_copy(
                update={
                    "events": [*state.events, event],
                    "last_sequence": event.sequence,
                    "last_activity_at": self._utc_now(),
                }
            )
            return await self._save_live_state(record, updated)

    async def record_progress(
        self, session_id: str, progress: CompetencyProgress
    ) -> SessionRecord:
        lock = await self._lock_for(session_id)
        async with lock:
            record = await self._get_live(session_id)
            state = self._require_active(record, "record competency progress")
            updated = state.model_copy(
                update={
                    "competency_progress": [*state.competency_progress, progress],
                    "last_activity_at": self._utc_now(),
                }
            )
            return await self._save_live_state(record, updated)

    async def record_metric(
        self, session_id: str, metric: LiveLatencyMetric
    ) -> SessionRecord:
        lock = await self._lock_for(session_id)
        async with lock:
            record = await self._get_live(session_id)
            state = self._require_active(record, "record a latency metric")
            updated = state.model_copy(update={"metrics": [*state.metrics, metric]})
            return await self._save_live_state(record, updated)

    async def store_resumption_handle(
        self, session_id: str, handle: str
    ) -> SessionRecord:
        normalized = handle.strip()
        if not normalized:
            raise ConflictError("The resumption handle must not be blank.")
        lock = await self._lock_for(session_id)
        async with lock:
            record = await self._get_live(session_id)
            state = self._require_active(record, "store a resumption handle")
            updated = state.model_copy(
                update={
                    "resumption_handle": normalized,
                    "last_activity_at": self._utc_now(),
                }
            )
            return await self._save_live_state(record, updated)

    async def completion_decision(self, session_id: str) -> CompletionDecision:
        record = await self._get_live(session_id)
        state = record.live_state
        assert state is not None
        if state.status in _TERMINAL:
            raise self._state_conflict(record, "make a completion decision")

        now = self._utc_now()
        if state.hard_stop_at is not None and now >= state.hard_stop_at:
            return CompletionDecision(
                approved=True,
                reason="hard_stop_reached",
            )

        supported = {
            item.competency.casefold()
            for item in state.competency_progress
            if item.evidence_state == "supported"
        }
        required = [item.name for item in state.rubric_snapshot.competencies]
        remaining = [name for name in required if name.casefold() not in supported]
        if remaining:
            return CompletionDecision(
                approved=False,
                remaining_competencies=remaining,
                reason="required_competencies_remaining",
            )
        if state.wrap_up_at is not None and now < state.wrap_up_at:
            return CompletionDecision(
                approved=False,
                remaining_competencies=[],
                reason="minimum_duration_not_reached",
            )
        return CompletionDecision(
            approved=True,
            reason="required_competencies_supported",
        )

    async def finish(self, session_id: str, *, reason: str) -> SessionRecord:
        lock = await self._lock_for(session_id)
        async with lock:
            record = await self._get_live(session_id)
            state = record.live_state
            assert state is not None
            if state.status == LiveInterviewStatus.SEALED:
                return record
            if state.status == LiveInterviewStatus.FAILED:
                raise self._state_conflict(record, "finish")
            if state.started_at is None:
                raise self._state_conflict(record, "finish")

            normalized_reason = reason.strip()
            if not normalized_reason:
                raise ConflictError("A finish reason is required.")
            now = self._utc_now()
            if state.status != LiveInterviewStatus.FINISHING:
                state = state.model_copy(
                    update={
                        "status": LiveInterviewStatus.FINISHING,
                        "last_activity_at": now,
                        "termination_reason": normalized_reason,
                    }
                )
                record = await self._save_live_state(record, state)

            transcript = project_live_transcript(
                interview_id=state.interview_id,
                candidate_id=record.candidate_id,
                job_id=record.job_id,
                started_at=state.started_at,
                ended_at=now,
                events=state.events,
            )
            await self._transcripts.save(transcript)
            sealed = state.model_copy(
                update={
                    "status": LiveInterviewStatus.SEALED,
                    "last_activity_at": now,
                    "termination_reason": state.termination_reason or normalized_reason,
                }
            )
            return await self._save_live_state(record, sealed)

    async def fail(self, session_id: str, *, category: str) -> SessionRecord:
        normalized = category.strip()
        if not normalized:
            raise ConflictError("A failure category is required.")
        lock = await self._lock_for(session_id)
        async with lock:
            record = await self._get_live(session_id)
            state = record.live_state
            assert state is not None
            if state.status == LiveInterviewStatus.FAILED:
                return record
            if state.status == LiveInterviewStatus.SEALED:
                raise self._state_conflict(record, "fail")
            failed = state.model_copy(
                update={
                    "status": LiveInterviewStatus.FAILED,
                    "last_activity_at": self._utc_now(),
                    "failure_category": normalized,
                }
            )
            return await self._save_live_state(record, failed)

    async def _get_live(self, session_id: str) -> SessionRecord:
        record = await self._sessions.get(session_id)
        if record is None:
            raise NotFoundError(
                "The live interview session was not found.",
                context={"session_id": session_id},
            )
        if record.interview_mode != "gemini_live" or record.live_state is None:
            raise ConflictError(
                "This operation requires a Gemini Live interview session.",
                context={"session_id": session_id},
            )
        return record

    def _require_active(
        self, record: SessionRecord, operation: str
    ) -> LiveInterviewState:
        state = record.live_state
        assert state is not None
        if state.status not in _ACTIVE:
            raise self._state_conflict(record, operation)
        return state

    async def _save_live_state(
        self, record: SessionRecord, state: LiveInterviewState
    ) -> SessionRecord:
        return await self._sessions.save(
            record.model_copy(
                update={
                    "status": _STATUS_MAP[state.status],
                    "live_state": state,
                    "termination_reason": (
                        state.termination_reason or state.failure_category
                    ),
                }
            )
        )

    def _state_conflict(self, record: SessionRecord, operation: str) -> ConflictError:
        assert record.live_state is not None
        return ConflictError(
            f"Cannot {operation} while the live interview is "
            f"{record.live_state.status.value}.",
            context={"session_id": record.session_id},
        )

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


__all__ = ["LiveInterviewService"]
