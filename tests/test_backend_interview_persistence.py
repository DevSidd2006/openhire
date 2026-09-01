"""
Chunk 3: persistent interview backend.

Covers what changed: `SessionRecord` now embeds the runner's COMPLETE
`InterviewState` plus its own private `job_description`/`parsed_resume`
snapshot (repositories/interfaces.py), `InterviewSessionRunner.rehydrate`
reconstructs a live runner from exactly that (utils/interview_session.py -
additive only, no existing method touched), `SessionRegistry.restore_session`
puts a rehydrated runner back under its ORIGINAL session_id
(api/registry.py - additive only), and `InterviewService.get_runner`
(services/interview_service.py) falls back to restoration when the runtime
registry doesn't have a session_id.

"Process restart" is simulated the same way throughout: build a FRESH,
empty `SessionRegistry` (and, for the HTTP tests, swap it onto
`app.state.registry`) while reusing the SAME `SessionRepository`/
`TranscriptRepository` instances - exactly what survives a real restart
(the repositories) versus what doesn't (the in-process registry).

No agent, provider, or engine file is modified by this chunk, and none of
these tests need a real LLM: everything goes through `ScriptedLLMProvider`
or the default mock provider, exactly like every other test in this suite.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agents.interviewer.agent import InterviewerAgent
from api.app import create_app
from api.registry import SessionNotFoundError, SessionRegistry
from repositories.memory import InMemorySessionRepository, InMemoryTranscriptRepository
from schemas.job import Competency, JobDescription
from schemas.resume import ParsedResume
from services.interview_service import InterviewService
from tests.fakes import ScriptedLLMProvider
from utils.interview_session import (
    InterviewSessionError,
    InterviewSessionRunner,
    SessionStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job(comps=None, job_id="job_persist"):
    comps = comps or {"Python": 0.5, "SQL": 0.5}
    return JobDescription(
        job_id=job_id, title="Backend Engineer", description="Test role",
        competencies=[Competency(name=n, weight=w) for n, w in comps.items()],
    )


def _resume(candidate_id="cand_persist"):
    return ParsedResume(candidate_id=candidate_id, candidate_name="Test Candidate",
                        skills=["Python", "SQL"])


def _question_json(text, qtype="initial", difficulty="medium"):
    return json.dumps({
        "question_text": text, "question_type": qtype, "difficulty": difficulty,
        "reason": "x", "expected_duration_seconds": 60,
    })


def _intro_json(text="Welcome! Tell me about yourself."):
    return _question_json(text, qtype="introduction", difficulty="easy")


def _eval_json(score=8.0, confidence=0.9, status="supported"):
    return json.dumps({
        "score": score, "confidence": confidence, "evidence_status": status,
        "is_vague": False, "missing_detail": None, "explanation": "x",
    })


def _scripted_interviewer(*question_texts, hang_seconds=None):
    script = [_intro_json()]
    for text in question_texts:
        script.extend([_question_json(text), _eval_json()])
    return lambda: InterviewerAgent(
        llm_provider=ScriptedLLMProvider(script=script, hang_seconds=hang_seconds)
    )


def _service(*, registry=None, sessions=None, transcripts=None, interviewer_factory=None):
    return InterviewService(
        registry=registry or SessionRegistry(),
        session_repository=sessions or InMemorySessionRepository(),
        transcript_repository=transcripts or InMemoryTranscriptRepository(),
        interviewer_factory=interviewer_factory or _scripted_interviewer("Q1?", "Q2?", "Q3?"),
    )


async def _answer_intro(service, session_id, text="Hi, nice to meet you."):
    return await service.submit_answer(session_id, text)


def _job_payload(job_id="job_http"):
    return {
        "job_id": job_id, "title": "Backend Engineer", "description": "Test role",
        "competencies": [{"name": "Python", "weight": 0.5}, {"name": "SQL", "weight": 0.5}],
    }


def _resume_payload(candidate_id="cand_http"):
    return {"candidate_id": candidate_id, "candidate_name": "Test Candidate",
            "skills": ["Python", "SQL"]}


def _create_payload(candidate_id="cand_http", job_id="job_http", **extra):
    payload = {
        "candidate_id": candidate_id, "job_id": job_id,
        "job_description": _job_payload(job_id), "parsed_resume": _resume_payload(candidate_id),
    }
    payload.update(extra)
    return payload


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def _simulate_restart(app) -> None:
    """Evict every live runner while keeping the repositories - exactly
    what a real process restart does: the registry (runtime-only) is lost,
    the repositories (durable) are not."""
    app.state.registry = SessionRegistry()


# ---------------------------------------------------------------------------
# 1/4. Session creation persistence - full state, not just a summary
# ---------------------------------------------------------------------------

class TestSessionCreationPersistence:
    @pytest.mark.asyncio
    async def test_created_session_record_embeds_full_state_and_snapshot(self):
        sessions = InMemorySessionRepository()
        service = _service(sessions=sessions)
        session_id, runner = await service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_persist",
        )

        record = await sessions.get(session_id)
        assert record is not None
        assert record.interview_id == runner.interview_id
        assert record.state is not None
        assert record.state.current_question is not None
        assert record.job_description == runner.job_description
        assert record.parsed_resume == runner.parsed_resume

    @pytest.mark.asyncio
    async def test_answer_persistence_grows_the_stored_exchanges(self):
        """Step 4/5: every answer that matters must have a persistence
        path - the full InterviewState (including `exchanges`) is what
        carries it, not a side table."""
        sessions = InMemorySessionRepository()
        service = _service(sessions=sessions)
        session_id, runner = await service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_persist",
        )
        await _answer_intro(service, session_id)
        await service.submit_answer(session_id, "A detailed answer about Python.")

        record = await sessions.get(session_id)
        assert len(record.state.exchanges) == 2
        assert record.state.exchanges[1][1].answer_text == "A detailed answer about Python."
        assert record.questions_answered == 1


# ---------------------------------------------------------------------------
# 2/3/14. Session retrieval, restoration/recovery, registry vs persisted state
# ---------------------------------------------------------------------------

class TestSessionRestoration:
    @pytest.mark.asyncio
    async def test_get_runner_restores_an_active_session_after_eviction(self):
        sessions = InMemorySessionRepository()
        registry = SessionRegistry()
        service = _service(registry=registry, sessions=sessions)
        session_id, original_runner = await service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_persist",
        )
        await _answer_intro(service, session_id)
        await service.submit_answer(session_id, "First answer.")

        # Simulate a restart: a BRAND NEW registry, same repositories - the
        # runtime registry is what does not survive a restart.
        fresh_registry = SessionRegistry()
        restored_service = _service(registry=fresh_registry, sessions=sessions)

        restored = await restored_service.get_runner(session_id)
        assert restored is not original_runner
        assert restored.status == SessionStatus.ACTIVE
        assert restored.interview_id == original_runner.interview_id
        assert restored.get_state().questions_answered == 1
        assert restored.get_current_question() is not None

    @pytest.mark.asyncio
    async def test_restored_session_can_continue_being_answered(self):
        """The whole point of restoration: the SAME interview continues,
        it does not restart from question one."""
        sessions = InMemorySessionRepository()
        service = _service(registry=SessionRegistry(), sessions=sessions)
        session_id, _ = await service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_persist",
            max_questions=3,
        )
        await _answer_intro(service, session_id)
        await service.submit_answer(session_id, "Answer one.")

        restored_service = _service(registry=SessionRegistry(), sessions=sessions)
        result = await restored_service.submit_answer(session_id, "Answer two, after restart.")

        assert result.question.question_text != "Q1?"  # answered the SECOND question, not a new "first"
        record = await sessions.get(session_id)
        assert record.state.questions_answered == 2
        assert [a.answer_text for _, a in record.state.exchanges] == [
            "Hi, nice to meet you.",
            "Answer one.", "Answer two, after restart.",
        ]

    @pytest.mark.asyncio
    async def test_get_runner_restores_a_sealed_session_and_rebuilds_its_transcript(self):
        """Step 7/8: a SEALED session must be restorable too (e.g. so a
        client can still GET it, or so a failed transcript write can still
        be retried) - and `_seal()` (unmodified) is what rebuilds the
        transcript, not a second implementation."""
        sessions = InMemorySessionRepository()
        service = _service(registry=SessionRegistry(), sessions=sessions, interviewer_factory=_scripted_interviewer("Q1?"))
        session_id, runner = await service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_persist",
            max_questions=1,
        )
        await _answer_intro(service, session_id)
        await service.submit_answer(session_id, "The only answer.")
        assert runner.status == SessionStatus.SEALED
        original_transcript = runner.get_transcript()

        restored_service = _service(registry=SessionRegistry(), sessions=sessions)
        restored = await restored_service.get_runner(session_id)

        assert restored.status == SessionStatus.SEALED
        restored_transcript = restored.get_transcript()
        assert restored_transcript.interview_id == original_transcript.interview_id
        assert restored_transcript.is_sealed is True
        assert len(restored_transcript.exchanges) == len(original_transcript.exchanges)
        assert restored_transcript.exchanges[1][1].answer_text == "The only answer."

    @pytest.mark.asyncio
    async def test_restoration_is_immune_to_the_job_being_edited_afterward(self):
        """The runner's OWN snapshot is what gets restored, not whatever
        JobRepository/CandidateRepository might contain by then - this
        service has no dependency on either."""
        sessions = InMemorySessionRepository()
        original_job = _job({"Python": 1.0})
        service = _service(registry=SessionRegistry(), sessions=sessions)
        session_id, _ = await service.create_session(
            job_description=original_job, parsed_resume=_resume(), candidate_id="cand_persist",
        )

        restored_service = _service(registry=SessionRegistry(), sessions=sessions)
        restored = await restored_service.get_runner(session_id)
        assert restored.job_description.competencies[0].name == "Python"
        assert restored.job_description == original_job

    @pytest.mark.asyncio
    async def test_unknown_session_id_is_still_not_found_after_restoration_is_added(self):
        service = _service()
        with pytest.raises(SessionNotFoundError):
            await service.get_runner("never-existed")

    @pytest.mark.asyncio
    async def test_a_record_with_no_persisted_state_cannot_be_restored(self):
        """A record missing what restoration needs (e.g. one built by hand
        without `state`, as several Chunk 1 tests still do) must fail the
        same way an unknown session_id does - never a different error."""
        from repositories.interfaces import SessionRecord

        sessions = InMemorySessionRepository()
        await sessions.save(SessionRecord(
            session_id="bare", candidate_id="c1", job_id="j1", status=SessionStatus.ACTIVE,
        ))
        service = _service(sessions=sessions)
        with pytest.raises(SessionNotFoundError):
            await service.get_runner("bare")

    @pytest.mark.asyncio
    async def test_runtime_registry_is_consulted_before_restoration_is_attempted(self):
        """Step 10: the registry remains the fast path for a session that
        IS resident - restoration must only be a fallback, never bypass an
        already-live runner (which would silently replace it and drop its
        idempotency cache / in-flight lock)."""
        registry = SessionRegistry()
        sessions = InMemorySessionRepository()
        service = _service(registry=registry, sessions=sessions)
        session_id, live_runner = await service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_persist",
        )

        resolved = await service.get_runner(session_id)
        assert resolved is live_runner


# ---------------------------------------------------------------------------
# 12/13. FAILED-state persistence (a genuine Chunk-1 gap this chunk closes)
# ---------------------------------------------------------------------------

class TestFailedStatePersistence:
    @pytest.mark.asyncio
    async def test_a_failed_evaluation_persists_the_failed_status(self):
        """Previously: runner.submit_answer() raising skipped
        _persist_record entirely, so a FAILED session looked ACTIVE forever
        in storage. Now the transition is captured before the exception
        propagates."""
        sessions = InMemorySessionRepository()
        broken_interviewer = lambda: InterviewerAgent(
            llm_provider=ScriptedLLMProvider(script=[_intro_json(), _question_json("Q1?"), RuntimeError("LLM exploded")])
        )
        service = _service(sessions=sessions, interviewer_factory=broken_interviewer)
        session_id, runner = await service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_persist",
        )
        await _answer_intro(service, session_id)

        with pytest.raises(InterviewSessionError):
            await service.submit_answer(session_id, "This answer's evaluation will fail.")

        assert runner.status == SessionStatus.FAILED
        record = await sessions.get(session_id)
        assert record.status == SessionStatus.FAILED


# ---------------------------------------------------------------------------
# 9. Application -> Interview linkage on the persisted record
# ---------------------------------------------------------------------------

class TestApplicationLinkagePersistence:
    @pytest.mark.asyncio
    async def test_application_id_is_stored_and_survives_every_later_save(self):
        """The real bug this guards against: SessionRecord.from_runner
        knows nothing about applications, so a naive re-save on every turn
        would silently reset application_id back to None."""
        sessions = InMemorySessionRepository()
        service = _service(sessions=sessions, interviewer_factory=_scripted_interviewer("Q1?", "Q2?"))
        session_id, _ = await service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_persist",
            application_id="app_xyz",
        )
        assert (await sessions.get(session_id)).application_id == "app_xyz"

        await service.submit_answer(session_id, "An answer.")
        assert (await sessions.get(session_id)).application_id == "app_xyz"

    @pytest.mark.asyncio
    async def test_application_id_is_none_when_not_supplied(self):
        sessions = InMemorySessionRepository()
        service = _service(sessions=sessions)
        session_id, _ = await service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_persist",
        )
        assert (await sessions.get(session_id)).application_id is None


# ---------------------------------------------------------------------------
# 6. Voice-answer persistence and restoration
# ---------------------------------------------------------------------------

class TestVoiceAnswerPersistence:
    @pytest.mark.asyncio
    async def test_record_turn_outcome_persists_the_full_state(self):
        """utils/voice_turn.py is untouched - this only verifies the SAME
        full-state persistence a typed answer gets also applies to a voice
        turn recorded via record_turn_outcome (Chunk 1)."""
        sessions = InMemorySessionRepository()
        service = _service(sessions=sessions)
        session_id, runner = await service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_persist",
        )
        # Simulate what utils/voice_turn.py already does: call the runner's
        # OWN submit_answer directly (voice orchestration, unmodified),
        # then hand the advanced runner to the service to record.
        await runner.submit_answer("Spoken answer, transcribed.")
        await service.record_turn_outcome(session_id, runner)

        record = await sessions.get(session_id)
        assert record.state is not None
        assert len(record.state.exchanges) == 1
        assert record.state.exchanges[0][1].answer_text == "Spoken answer, transcribed."

    @pytest.mark.asyncio
    async def test_a_voice_driven_session_is_restorable_like_any_other(self):
        sessions = InMemorySessionRepository()
        service = _service(registry=SessionRegistry(), sessions=sessions)
        session_id, runner = await service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_persist",
        )
        await runner.submit_answer("Hi, nice to meet you.")
        await runner.submit_answer("Spoken answer.")
        await service.record_turn_outcome(session_id, runner)

        restored_service = _service(registry=SessionRegistry(), sessions=sessions)
        restored = await restored_service.get_runner(session_id)
        assert restored.get_state().questions_answered == 1


# ---------------------------------------------------------------------------
# 7/8. Transcript persistence, failure, and retry - including after restart
# ---------------------------------------------------------------------------

class TestTranscriptPersistenceAcrossRestart:
    @pytest.mark.asyncio
    async def test_retry_after_restart_recovers_a_transcript_write_that_failed_live(self):
        """The strongest version of Chunk 1's retry guarantee: even if the
        ORIGINAL process crashed before a retry could happen, a fresh
        process can still recover the transcript, because the full
        InterviewState needed to rebuild it (via the restored runner's
        OWN _seal()) was already durably saved."""
        sessions = InMemorySessionRepository()
        transcripts = InMemoryTranscriptRepository()
        service = _service(
            sessions=sessions, transcripts=transcripts,
            interviewer_factory=_scripted_interviewer("Q1?"),
        )
        session_id, runner = await service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_persist",
            max_questions=1,
        )
        await _answer_intro(service, session_id)

        async def broken_save(transcript):
            raise RuntimeError("transcript store unreachable")

        transcripts.save = broken_save  # type: ignore[assignment]
        await service.submit_answer(session_id, "The only answer.")
        assert runner.status == SessionStatus.SEALED
        assert await transcripts.get(runner.interview_id) is None  # write failed, as expected

        # "Restart": fresh registry, repository recovers, and this is a
        # brand-new InterviewService (no in-memory knowledge of the failure).
        transcripts.save = InMemoryTranscriptRepository.save.__get__(transcripts)
        restored_service = _service(registry=SessionRegistry(), sessions=sessions, transcripts=transcripts)
        restored = await restored_service.get_runner(session_id)

        recovered = await restored_service.retry_transcript_persistence(session_id, restored)
        assert recovered is True
        stored = await transcripts.get(runner.interview_id)
        assert stored is not None and stored.is_sealed is True
        assert len(stored.exchanges) == 2


# ---------------------------------------------------------------------------
# 11/15. Concurrency: duplicate finish, submit-vs-finish race, restore race
# ---------------------------------------------------------------------------

class TestConcurrency:
    @pytest.mark.asyncio
    async def test_two_concurrent_finish_requests_seal_exactly_once(self):
        """The runner's own lock (unmodified) already serializes this - this
        pins that the guarantee still holds with the new persistence calls
        wrapped around it."""
        interviewer_factory = _scripted_interviewer("Q1?", "Q2?", hang_seconds=0.02)
        service = _service(interviewer_factory=interviewer_factory)
        session_id, runner = await service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_persist",
        )

        results = await asyncio.gather(
            service.finish_session(session_id),
            service.finish_session(session_id),
            return_exceptions=True,
        )
        successes = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, Exception)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], InterviewSessionError)
        assert runner.status == SessionStatus.SEALED
        assert runner.get_transcript().is_sealed is True

    @pytest.mark.asyncio
    async def test_submit_answer_racing_finish_never_corrupts_the_session(self):
        """Both submit_answer and request_finish acquire the SAME
        runner-owned lock (utils/interview_session.py, unmodified), so the
        lock fully serializes them rather than letting them interleave -
        whichever wins the lock runs to completion first. Both CAN
        legitimately succeed in sequence (finish is always valid on an
        ACTIVE session, which is exactly what a just-answered, not-yet-
        finished interview still is) - what must never happen is
        corruption: a duplicated exchange, an answer recorded twice, or a
        transcript sealed in an inconsistent state.
        """
        interviewer_factory = _scripted_interviewer("Q1?", "Q2?", hang_seconds=0.02)
        service = _service(interviewer_factory=interviewer_factory)
        session_id, runner = await service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_persist",
        )

        results = await asyncio.gather(
            service.submit_answer(session_id, "Racing answer."),
            service.finish_session(session_id),
            return_exceptions=True,
        )
        # Whichever interleaving occurred, the session must end up SEALED
        # (finish_session either sealed it directly, or found it already
        # sealed and raised cleanly) and internally consistent - never
        # left ACTIVE with a lost update, and never corrupted.
        assert runner.status == SessionStatus.SEALED
        for result in results:
            if isinstance(result, Exception):
                assert isinstance(result, InterviewSessionError)

        state = runner.get_state()
        question_ids = [q.question_id for q, _ in state.exchanges]
        answer_ids = [a.question_id for _, a in state.exchanges]
        assert len(question_ids) == len(set(question_ids))
        assert len(answer_ids) == len(set(answer_ids))
        assert runner.get_transcript().is_sealed is True

    @pytest.mark.asyncio
    async def test_concurrent_restoration_of_the_same_session_converges_on_one_runner(self):
        """SessionRegistry.restore_session must never let two concurrent
        rehydrations both win - exactly one canonical runner object per
        session_id, or the runner's own concurrency guarantees (its lock,
        its idempotency cache) would silently apply to two different
        objects instead of one."""
        sessions = InMemorySessionRepository()
        service = _service(sessions=sessions)
        session_id, _ = await service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_persist",
        )

        fresh_registry = SessionRegistry()
        service_a = _service(registry=fresh_registry, sessions=sessions)
        service_b = _service(registry=fresh_registry, sessions=sessions)

        runner_a, runner_b = await asyncio.gather(
            service_a.get_runner(session_id), service_b.get_runner(session_id),
        )
        assert runner_a is runner_b


# ---------------------------------------------------------------------------
# 16. Existing API compatibility - the documented routes, unchanged
# ---------------------------------------------------------------------------

class TestExistingApiCompatibility:
    def test_full_session_lifecycle_through_the_http_api_is_unaffected(self, client):
        created = client.post("/sessions", json=_create_payload(job_id="job_compat", candidate_id="cand_compat"))
        assert created.status_code == 201
        sid = created.json()["session_id"]

        state = client.get(f"/sessions/{sid}")
        assert state.status_code == 200

        answer = client.post(f"/sessions/{sid}/answers", json={"answer_text": "An answer."})
        assert answer.status_code == 200

        finish = client.post(f"/sessions/{sid}/finish")
        assert finish.status_code in (200, 409)  # 409 if it had already auto-sealed

    def test_get_after_simulated_restart_returns_the_same_session(self, client, app):
        created = client.post("/sessions", json=_create_payload(job_id="job_compat2", candidate_id="cand_compat2"))
        sid = created.json()["session_id"]

        _simulate_restart(app)

        state = client.get(f"/sessions/{sid}")
        assert state.status_code == 200
        assert state.json()["session_id"] == sid

    def test_answers_after_simulated_restart_continue_the_same_interview(self, client, app):
        app.state.interviewer_factory = _scripted_interviewer("Q1?", "Q2?", "Q3?")
        created = client.post(
            "/sessions",
            json=_create_payload(job_id="job_compat3", candidate_id="cand_compat3", max_questions=3),
        )
        sid = created.json()["session_id"]
        client.post(f"/sessions/{sid}/answers", json={"answer_text": "Hi, nice to meet you."})
        client.post(f"/sessions/{sid}/answers", json={"answer_text": "First answer."})

        _simulate_restart(app)

        second = client.post(f"/sessions/{sid}/answers", json={"answer_text": "Second answer, after restart."})
        assert second.status_code == 200
        state = client.get(f"/sessions/{sid}").json()
        assert state["progress"]["questions_answered"] == 2

    def test_unknown_session_id_is_still_404(self, client):
        response = client.get("/sessions/never-existed")
        assert response.status_code == 404
        assert response.json()["error"] == "session_not_found"

    def test_application_bridge_from_chunk_2_is_unaffected(self, client):
        """CreateSessionResponse.application_id stays None when omitted -
        Chunk 3 adds no new required field to the request or response."""
        created = client.post("/sessions", json=_create_payload(job_id="job_compat4", candidate_id="cand_compat4"))
        assert created.status_code == 201
        assert created.json()["application_id"] is None
