from datetime import datetime, timedelta, timezone

import pytest

from core.errors import ConflictError, NotFoundError
from repositories.interfaces import RepositoryError, SessionRecord
from repositories.memory import InMemorySessionRepository, InMemoryTranscriptRepository
from schemas.job import Competency, JobDescription
from schemas.live_interview import (
    CompetencyProgress,
    LiveInterviewStatus,
    LiveLatencyMetric,
    LiveTranscriptEvent,
)
from schemas.resume import ParsedResume
from services.live_interview_service import LiveInterviewService
from tests.rubric_fixtures import approved_rubric
from utils.interview_session import SessionStatus


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        return self.value


def job():
    return JobDescription(
        job_id="job_001",
        title="Backend Engineer",
        description="Build APIs",
        competencies=[
            Competency(name="Python", weight=0.5),
            Competency(name="System design", weight=0.5),
        ],
    )


def resume():
    return ParsedResume(
        candidate_id="cand_1", candidate_name="Candidate", skills=["Python"]
    )


def live_event(sequence: int, speaker: str, text: str, **updates):
    values = {
        "event_id": f"evt_{sequence}",
        "sequence": sequence,
        "speaker": speaker,
        "text": text,
        "started_at_ms": sequence * 1_000,
        "ended_at_ms": sequence * 1_000 + 500,
    }
    values.update(updates)
    return LiveTranscriptEvent(**values)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def session_repository():
    return InMemorySessionRepository()


@pytest.fixture
def transcript_repository():
    return InMemoryTranscriptRepository()


@pytest.fixture
def service(session_repository, transcript_repository, clock):
    return LiveInterviewService(
        session_repository,
        transcript_repository,
        now=clock,
    )


async def create_started_session(service: LiveInterviewService) -> SessionRecord:
    record = await service.create_session(
        job_description=job(),
        parsed_resume=resume(),
        rubric_snapshot=approved_rubric(),
        candidate_id="cand_1",
    )
    return await service.start(record.session_id)


@pytest.mark.asyncio
async def test_create_and_start_snapshot_inputs_and_set_time_budget(service, clock):
    record = await service.create_session(
        job_description=job(),
        parsed_resume=resume(),
        rubric_snapshot=approved_rubric(),
        candidate_id="cand_1",
        application_id="app_1",
    )
    assert record.interview_mode == "gemini_live"
    assert record.status == SessionStatus.CREATED
    assert record.state is None
    assert record.live_state.status == LiveInterviewStatus.CREATED
    assert record.live_state.wrap_up_at is None

    started = await service.start(record.session_id)
    assert started.status == SessionStatus.ACTIVE
    assert started.live_state.started_at == clock.value
    assert started.live_state.wrap_up_at == clock.value + timedelta(minutes=12)
    assert started.live_state.hard_stop_at == clock.value + timedelta(minutes=15)


@pytest.mark.asyncio
async def test_append_event_is_ordered_idempotent_and_persisted(service):
    record = await create_started_session(service)
    first = live_event(1, "candidate", "My answer")
    stored = await service.append_event(record.session_id, first)
    assert stored.live_state.last_sequence == 1

    replay = await service.append_event(record.session_id, first)
    assert len(replay.live_state.events) == 1

    with pytest.raises(ConflictError, match="reused"):
        await service.append_event(
            record.session_id,
            first.model_copy(update={"text": "different"}),
        )
    with pytest.raises(ConflictError, match="sequence"):
        await service.append_event(
            record.session_id, live_event(3, "candidate", "gap")
        )


@pytest.mark.asyncio
async def test_append_rejects_reversed_backward_and_post_deadline_timestamps(service):
    record = await create_started_session(service)
    await service.append_event(record.session_id, live_event(1, "candidate", "one"))

    with pytest.raises(ConflictError, match="reversed"):
        await service.append_event(
            record.session_id,
            live_event(
                2,
                "candidate",
                "bad",
                started_at_ms=3_000,
                ended_at_ms=2_000,
            ),
        )
    with pytest.raises(ConflictError, match="backward"):
        await service.append_event(
            record.session_id,
            live_event(
                2,
                "candidate",
                "bad",
                started_at_ms=900,
                ended_at_ms=1_400,
            ),
        )
    with pytest.raises(ConflictError, match="hard stop"):
        await service.append_event(
            record.session_id,
            live_event(
                2,
                "candidate",
                "late",
                started_at_ms=905_001,
                ended_at_ms=905_002,
            ),
        )


@pytest.mark.asyncio
async def test_progress_metrics_resumption_and_reconnect_are_persisted(service):
    record = await create_started_session(service)
    record = await service.record_progress(
        record.session_id,
        CompetencyProgress(competency="Python", evidence_state="supported"),
    )
    record = await service.record_metric(
        record.session_id,
        LiveLatencyMetric(name="turn_to_first_audio", duration_ms=420),
    )
    record = await service.store_resumption_handle(record.session_id, "handle-1")
    record = await service.reconnecting(record.session_id)

    assert record.live_state.resumption_handle == "handle-1"
    assert record.live_state.reconnect_count == 1
    assert record.live_state.status == LiveInterviewStatus.RECONNECTING
    resumed = await service.start(record.session_id)
    assert resumed.live_state.status == LiveInterviewStatus.ACTIVE


@pytest.mark.asyncio
async def test_completion_decision_requires_all_rubric_competencies(service, clock):
    record = await create_started_session(service)
    decision = await service.completion_decision(record.session_id)
    assert decision.approved is False
    assert decision.remaining_competencies == [
        "Python",
        "System design",
        "Ownership",
    ]

    for competency in decision.remaining_competencies:
        await service.record_progress(
            record.session_id,
            CompetencyProgress(
                competency=competency,
                evidence_state="supported",
            ),
        )
    assert (await service.completion_decision(record.session_id)).approved is True

    other = await create_started_session(service)
    clock.value += timedelta(minutes=15)
    hard_stop = await service.completion_decision(other.session_id)
    assert hard_stop.approved is True
    assert hard_stop.reason == "hard_stop_reached"


@pytest.mark.asyncio
async def test_finish_projects_persists_and_seals(service, transcript_repository):
    record = await create_started_session(service)
    await service.append_event(
        record.session_id, live_event(1, "interviewer", "Why Python?")
    )
    await service.append_event(
        record.session_id,
        live_event(2, "candidate", "For its ecosystem."),
    )

    result = await service.finish(record.session_id, reason="time_budget_complete")
    assert result.status == SessionStatus.SEALED
    assert result.live_state.status == LiveInterviewStatus.SEALED
    transcript = await transcript_repository.get(record.interview_id)
    assert transcript.is_sealed is True
    assert transcript.exchanges[0][1].answer_text == "For its ecosystem."
    assert await service.finish(record.session_id, reason="retry") == result


@pytest.mark.asyncio
async def test_finish_persistence_failure_leaves_retryable_finishing_state(
    session_repository, clock
):
    class FailingOnceTranscriptRepository(InMemoryTranscriptRepository):
        def __init__(self):
            super().__init__()
            self.fail = True

        async def save(self, transcript):
            if self.fail:
                self.fail = False
                raise RepositoryError("temporary failure")
            await super().save(transcript)

    transcripts = FailingOnceTranscriptRepository()
    service = LiveInterviewService(session_repository, transcripts, now=clock)
    record = await create_started_session(service)

    with pytest.raises(RepositoryError):
        await service.finish(record.session_id, reason="complete")
    persisted = await session_repository.get(record.session_id)
    assert persisted.status == SessionStatus.FINISHING
    assert persisted.live_state.status == LiveInterviewStatus.FINISHING

    sealed = await service.finish(record.session_id, reason="retry")
    assert sealed.status == SessionStatus.SEALED


@pytest.mark.asyncio
async def test_fail_is_idempotent_and_blocks_further_updates(service):
    record = await create_started_session(service)
    failed = await service.fail(record.session_id, category="provider_error")
    assert failed.status == SessionStatus.FAILED
    assert failed.termination_reason == "provider_error"
    assert await service.fail(record.session_id, category="ignored") == failed
    with pytest.raises(ConflictError):
        await service.append_event(
            record.session_id, live_event(1, "candidate", "too late")
        )


@pytest.mark.asyncio
async def test_unknown_and_adaptive_sessions_are_rejected(
    service, session_repository
):
    with pytest.raises(NotFoundError):
        await service.get("missing")

    adaptive = await session_repository.save(
        SessionRecord(
            session_id="adaptive",
            candidate_id="cand_1",
            job_id="job_001",
            status=SessionStatus.CREATED,
        )
    )
    with pytest.raises(ConflictError):
        await service.start(adaptive.session_id)
