"""
Chunk 4: evaluation orchestration + backend integration.

Covers the backend integration built around the EXISTING, unmodified
evaluation agents (TechnicalEvaluatorAgent, BehavioralEvaluatorAgent,
ResumeAuditorAgent, IntegrityAgent, BiasCheckerAgent, ScoringAgent,
ReportGeneratorAgent) and their existing deterministic aggregation - no
agent, prompt, or scoring formula is reimplemented here. See
services/evaluation_service.py's module docstring for why these are called
directly rather than through `orchestration.graph.get_pipeline()`.

Background execution is tested via a small `RecordingDispatcher` (defined
below, test-only) that captures what would have been scheduled and lets a
test run it deterministically with `await dispatcher.run_all()` - this
exercises the REAL `EvaluationService._run` execution path, just without
depending on real `asyncio.create_task` timing. One test
(`TestBackgroundExecutionIsReal`) uses the REAL
`AsyncTaskEvaluationDispatcher` to prove the HTTP request itself never
blocks on the pipeline.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.errors import ConflictError, NotFoundError
from repositories.interfaces import Application, EvaluationStatus
from repositories.memory import (
    InMemoryApplicationRepository,
    InMemoryEvaluationRepository,
    InMemorySessionRepository,
    InMemoryTranscriptRepository,
)
from schemas.application import ApplicationStatus
from schemas.evaluation import MatchingScore
from schemas.job import Competency, JobDescription
from schemas.resume import ParsedResume
from services.evaluation_service import EvaluationAgentFactories, EvaluationService
from services.interview_service import InterviewService

# ---------------------------------------------------------------------------
# Test-only deterministic dispatcher
# ---------------------------------------------------------------------------

class RecordingDispatcher:
    """Captures scheduled work instead of running it immediately - a test
    calls `await run_all()` to execute it deterministically. Satisfies the
    real `EvaluationDispatcher` protocol; never used in production code."""

    def __init__(self) -> None:
        self.scheduled: list = []

    def schedule(self, evaluation_id, run) -> None:
        self.scheduled.append((evaluation_id, run))

    async def run_all(self) -> None:
        pending, self.scheduled = self.scheduled, []
        for _evaluation_id, run in pending:
            await run()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job(comps=None, job_id="job_eval"):
    comps = comps or {"Python": 0.5, "SQL": 0.5}
    return JobDescription(
        job_id=job_id, title="Backend Engineer", description="Test role",
        required_skills=["Python", "SQL"],
        competencies=[Competency(name=n, weight=w) for n, w in comps.items()],
    )


def _resume(candidate_id="cand_eval"):
    return ParsedResume(candidate_id=candidate_id, candidate_name="Test Candidate",
                        skills=["Python", "SQL"])


def _matching_score(candidate_id="cand_eval", job_id="job_eval") -> MatchingScore:
    return MatchingScore(
        match_id="match_eval_1", candidate_id=candidate_id, job_id=job_id,
        match_score=0.9, skill_matches=["Python", "SQL"], experience_match=0.9,
        skill_gap=0.1, explanation="strong match", shortlist_recommendation=True,
        confidence=0.9,
    )


def _question_json(text, qtype="initial", difficulty="medium"):
    return json.dumps({
        "question_text": text, "question_type": qtype, "difficulty": difficulty,
        "reason": "x", "expected_duration_seconds": 60,
    })


def _eval_json(score=8.0, confidence=0.9, status="supported"):
    return json.dumps({
        "score": score, "confidence": confidence, "evidence_status": status,
        "is_vague": False, "missing_detail": None, "explanation": "x",
    })


def _scripted_interviewer(*question_texts):
    from agents.interviewer.agent import InterviewerAgent
    from tests.fakes import ScriptedLLMProvider

    script = []
    for text in question_texts:
        script.extend([_question_json(text), _eval_json()])
    return lambda: InterviewerAgent(llm_provider=ScriptedLLMProvider(script=script))


class _Harness:
    """Bundles the repositories + services one evaluation test needs, all
    sharing the same in-memory backing stores - mirrors the pattern
    established in tests/test_backend_jobs_candidates_applications.py and
    tests/test_backend_interview_persistence.py."""

    def __init__(self, *, dispatcher=None, agent_factories=None):
        from api.registry import SessionRegistry

        self.sessions = InMemorySessionRepository()
        self.transcripts = InMemoryTranscriptRepository()
        self.applications = InMemoryApplicationRepository()
        self.evaluations = InMemoryEvaluationRepository()
        self.dispatcher = dispatcher or RecordingDispatcher()

        self.interview_service = InterviewService(
            registry=SessionRegistry(),
            session_repository=self.sessions,
            transcript_repository=self.transcripts,
            interviewer_factory=_scripted_interviewer("Q1?"),
        )
        self.evaluation_service = EvaluationService(
            evaluation_repository=self.evaluations,
            session_repository=self.sessions,
            transcript_repository=self.transcripts,
            application_repository=self.applications,
            dispatcher=self.dispatcher,
            agent_factories=agent_factories,
        )

    async def seed_shortlisted_application(
        self, *, application_id="app_eval", candidate_id="cand_eval", job_id="job_eval",
    ) -> Application:
        application = Application(
            application_id=application_id, job_id=job_id, candidate_id=candidate_id,
            status=ApplicationStatus.SHORTLISTED,
            matching_score=_matching_score(candidate_id, job_id),
        )
        return await self.applications.save(application)

    async def seed_sealed_session(
        self, *, application_id=None, candidate_id="cand_eval", job_id="job_eval", max_questions=1,
    ) -> str:
        session_id, runner = await self.interview_service.create_session(
            job_description=_job(job_id=job_id), parsed_resume=_resume(candidate_id),
            candidate_id=candidate_id, max_questions=max_questions, application_id=application_id,
        )
        while not runner.is_finished():
            result = await self.interview_service.submit_answer(session_id, "A detailed real answer.")
            if result.next_question is None:
                break
        assert runner.status.value == "sealed"
        return session_id


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 1/4/5/6. Creation, real pipeline invocation, success, result persistence
# ---------------------------------------------------------------------------

class TestEvaluationCreationAndSuccess:
    @pytest.mark.asyncio
    async def test_trigger_creates_a_pending_job_without_running_it(self):
        harness = _Harness()
        await harness.seed_shortlisted_application()
        session_id = await harness.seed_sealed_session(application_id="app_eval")

        job = await harness.evaluation_service.trigger_evaluation(session_id)
        assert job.status == EvaluationStatus.PENDING
        assert job.result is None
        assert harness.dispatcher.scheduled  # something was scheduled, not run

    @pytest.mark.asyncio
    async def test_running_the_scheduled_evaluation_produces_a_real_completed_report(self):
        """Step 4/5/6: the REAL agents ran (MockLLMProvider, deterministic),
        and the result is a genuine CandidateReport, persisted."""
        harness = _Harness()
        await harness.seed_shortlisted_application()
        session_id = await harness.seed_sealed_session(application_id="app_eval")

        job = await harness.evaluation_service.trigger_evaluation(session_id)
        await harness.dispatcher.run_all()

        stored = await harness.evaluations.get(job.evaluation_id)
        assert stored.status == EvaluationStatus.COMPLETED
        assert stored.result is not None
        assert stored.result.candidate_id == "cand_eval"
        assert stored.result.job_id == "job_eval"
        assert stored.result.scores.weighted_final_score >= 0
        assert stored.started_at is not None
        assert stored.completed_at is not None

    @pytest.mark.asyncio
    async def test_evaluation_job_correctly_links_application_interview_candidate_job(self):
        """Step 18."""
        harness = _Harness()
        await harness.seed_shortlisted_application(application_id="app_link")
        session_id = await harness.seed_sealed_session(application_id="app_link")

        job = await harness.evaluation_service.trigger_evaluation(session_id)
        assert job.session_id == session_id
        assert job.application_id == "app_link"
        assert job.candidate_id == "cand_eval"
        assert job.job_id == "job_eval"
        assert job.interview_id  # non-empty


# ---------------------------------------------------------------------------
# 7/8/9. Idempotency: duplicate trigger, already running, already completed
# ---------------------------------------------------------------------------

class TestIdempotency:
    @pytest.mark.asyncio
    async def test_duplicate_trigger_returns_the_same_job_and_schedules_once(self):
        harness = _Harness()
        await harness.seed_shortlisted_application()
        session_id = await harness.seed_sealed_session(application_id="app_eval")

        first = await harness.evaluation_service.trigger_evaluation(session_id)
        second = await harness.evaluation_service.trigger_evaluation(session_id)
        assert first.evaluation_id == second.evaluation_id
        assert len(harness.dispatcher.scheduled) == 1

    @pytest.mark.asyncio
    async def test_trigger_while_running_returns_existing_untouched(self):
        harness = _Harness()
        await harness.seed_shortlisted_application()
        session_id = await harness.seed_sealed_session(application_id="app_eval")
        job = await harness.evaluation_service.trigger_evaluation(session_id)

        running = job.model_copy(update={"status": EvaluationStatus.RUNNING})
        await harness.evaluations.save(running)
        harness.dispatcher.scheduled.clear()

        again = await harness.evaluation_service.trigger_evaluation(session_id)
        assert again.status == EvaluationStatus.RUNNING
        assert harness.dispatcher.scheduled == []  # nothing re-scheduled

    @pytest.mark.asyncio
    async def test_trigger_after_completed_returns_existing_result_untouched(self):
        harness = _Harness()
        await harness.seed_shortlisted_application()
        session_id = await harness.seed_sealed_session(application_id="app_eval")
        job = await harness.evaluation_service.trigger_evaluation(session_id)
        await harness.dispatcher.run_all()
        completed = await harness.evaluations.get(job.evaluation_id)
        assert completed.status == EvaluationStatus.COMPLETED

        again = await harness.evaluation_service.trigger_evaluation(session_id)
        assert again.evaluation_id == job.evaluation_id
        assert again.status == EvaluationStatus.COMPLETED
        assert again.result is not None
        assert harness.dispatcher.scheduled == []  # never re-run a completed evaluation

    @pytest.mark.asyncio
    async def test_concurrent_duplicate_triggers_never_create_two_jobs(self):
        """The genuine race Step 6 cares about: two nearly-simultaneous
        triggers for the same session (duplicate finish, submit-vs-finish
        racing, a network retry) must converge on exactly one job."""
        harness = _Harness()
        await harness.seed_shortlisted_application()
        session_id = await harness.seed_sealed_session(application_id="app_eval")

        results = await asyncio.gather(
            harness.evaluation_service.trigger_evaluation(session_id),
            harness.evaluation_service.trigger_evaluation(session_id),
        )
        assert results[0].evaluation_id == results[1].evaluation_id
        assert len(harness.dispatcher.scheduled) == 1


# ---------------------------------------------------------------------------
# 10. Retry after failure
# ---------------------------------------------------------------------------

class TestRetry:
    @pytest.mark.asyncio
    async def test_retry_reruns_a_failed_evaluation_to_completion(self):
        harness = _Harness()
        await harness.seed_shortlisted_application()
        # No application_id linked - guarantees a FAILED evaluation (no
        # matching_score available), a clean way to get to FAILED without
        # needing a broken agent.
        session_id = await harness.seed_sealed_session(application_id=None)

        # Link it retroactively so the retry CAN succeed.
        job = await harness.evaluation_service.trigger_evaluation(session_id)
        await harness.dispatcher.run_all()
        failed = await harness.evaluations.get(job.evaluation_id)
        assert failed.status == EvaluationStatus.FAILED
        assert failed.error is not None

        # Patch the stored job to reference the (now-linked) application,
        # simulating the operator having fixed the underlying gap.
        relinked = failed.model_copy(update={"application_id": "app_eval"})
        await harness.evaluations.save(relinked)

        retried = await harness.evaluation_service.retry_evaluation(job.evaluation_id)
        assert retried.status == EvaluationStatus.PENDING
        assert retried.error is None
        await harness.dispatcher.run_all()

        final = await harness.evaluations.get(job.evaluation_id)
        assert final.status == EvaluationStatus.COMPLETED
        assert final.result is not None

    @pytest.mark.asyncio
    async def test_retry_is_refused_for_a_non_failed_evaluation(self):
        harness = _Harness()
        await harness.seed_shortlisted_application()
        session_id = await harness.seed_sealed_session(application_id="app_eval")
        job = await harness.evaluation_service.trigger_evaluation(session_id)  # PENDING

        with pytest.raises(ConflictError):
            await harness.evaluation_service.retry_evaluation(job.evaluation_id)

    @pytest.mark.asyncio
    async def test_retry_of_unknown_evaluation_is_not_found(self):
        harness = _Harness()
        with pytest.raises(NotFoundError):
            await harness.evaluation_service.retry_evaluation("never-existed")


# ---------------------------------------------------------------------------
# 11/12/13. Agent failure, provider failure, malformed result
# ---------------------------------------------------------------------------

class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_required_agent_failure_fails_the_whole_evaluation(self):
        """Technical evaluation is REQUIRED (ScoringAgent cannot run
        without it) - its failure must never produce a partial/fabricated
        score, matching orchestration/graph.py:node_score_candidates'
        own exclusion rule."""

        class _BrokenTechnicalEvaluator:
            async def run(self, **kwargs):
                return {"result": {"technical_evaluation": None, "error": "LLM exploded"}}

        harness = _Harness(agent_factories=EvaluationAgentFactories(
            technical_evaluator=lambda: _BrokenTechnicalEvaluator(),
        ))
        await harness.seed_shortlisted_application()
        session_id = await harness.seed_sealed_session(application_id="app_eval")

        job = await harness.evaluation_service.trigger_evaluation(session_id)
        await harness.dispatcher.run_all()

        failed = await harness.evaluations.get(job.evaluation_id)
        assert failed.status == EvaluationStatus.FAILED
        assert failed.result is None
        assert "technical evaluation failed" in failed.error

    @pytest.mark.asyncio
    async def test_provider_level_exception_is_captured_not_propagated(self):
        """A raw exception escaping an agent's run() (rather than the
        agent's own {"error": ...} envelope) must still be caught and
        recorded, never crash the background task."""

        class _CrashingAgent:
            async def run(self, **kwargs):
                raise RuntimeError("provider connection reset")

        harness = _Harness(agent_factories=EvaluationAgentFactories(
            behavioral_evaluator=lambda: _CrashingAgent(),
        ))
        await harness.seed_shortlisted_application()
        session_id = await harness.seed_sealed_session(application_id="app_eval")

        job = await harness.evaluation_service.trigger_evaluation(session_id)
        await harness.dispatcher.run_all()  # must not raise

        failed = await harness.evaluations.get(job.evaluation_id)
        assert failed.status == EvaluationStatus.FAILED
        assert "provider connection reset" in failed.error

    @pytest.mark.asyncio
    async def test_optional_agent_failure_is_non_fatal(self):
        """resume_audit/integrity/bias are OPTIONAL for the report - their
        failure must be recorded as a warning, not fail the evaluation."""

        class _BrokenIntegrity:
            async def run(self, **kwargs):
                return {"result": {"integrity_evaluation": None, "error": "integrity LLM failed"}}

        harness = _Harness(agent_factories=EvaluationAgentFactories(
            integrity=lambda: _BrokenIntegrity(),
        ))
        await harness.seed_shortlisted_application()
        session_id = await harness.seed_sealed_session(application_id="app_eval")

        job = await harness.evaluation_service.trigger_evaluation(session_id)
        await harness.dispatcher.run_all()

        completed = await harness.evaluations.get(job.evaluation_id)
        assert completed.status == EvaluationStatus.COMPLETED
        assert completed.result is not None
        assert any("integrity check failed" in w for w in completed.warnings)

    @pytest.mark.asyncio
    async def test_malformed_agent_result_is_treated_as_failure_not_a_crash(self):
        """An agent returning a 'success' envelope that is missing the key
        the service reads must be treated exactly like an explicit
        failure - never a KeyError/AttributeError escaping to the caller."""

        class _MalformedScoringAgent:
            async def run(self, **kwargs):
                return {"result": {"unexpected_key": "not what was asked for"}}

        harness = _Harness(agent_factories=EvaluationAgentFactories(
            scoring=lambda: _MalformedScoringAgent(),
        ))
        await harness.seed_shortlisted_application()
        session_id = await harness.seed_sealed_session(application_id="app_eval")

        job = await harness.evaluation_service.trigger_evaluation(session_id)
        await harness.dispatcher.run_all()  # must not raise

        failed = await harness.evaluations.get(job.evaluation_id)
        assert failed.status == EvaluationStatus.FAILED
        assert "scoring failed" in failed.error

    @pytest.mark.asyncio
    async def test_no_matching_score_fails_cleanly(self):
        """ScoringAgent requires a MatchingScore; a session with no
        application_id (or an application with none) has no source for
        one anywhere in this backend - a clear, explicit failure, not an
        AttributeError."""
        harness = _Harness()
        session_id = await harness.seed_sealed_session(application_id=None)

        job = await harness.evaluation_service.trigger_evaluation(session_id)
        await harness.dispatcher.run_all()

        failed = await harness.evaluations.get(job.evaluation_id)
        assert failed.status == EvaluationStatus.FAILED
        assert "matching score" in failed.error


# ---------------------------------------------------------------------------
# 14. Transcript unavailable
# ---------------------------------------------------------------------------

class TestTranscriptUnavailable:
    @pytest.mark.asyncio
    async def test_trigger_refuses_when_transcript_not_yet_persisted(self):
        """Simulates the Chunk 1 scenario: the transcript write failed at
        seal time and has not been retried yet - the same
        `_break_transcript_save` style already used in
        tests/test_backend_foundation.py, applied here so `trigger_evaluation`
        sees a sealed session with genuinely no transcript in storage."""
        harness = _Harness()
        await harness.seed_shortlisted_application()

        async def broken_save(transcript):
            raise RuntimeError("transcript store unreachable")

        harness.transcripts.save = broken_save  # type: ignore[assignment]
        session_id = await harness.seed_sealed_session(application_id="app_eval")

        assert await harness.transcripts.get((await harness.sessions.get(session_id)).interview_id) is None

        with pytest.raises(ConflictError):
            await harness.evaluation_service.trigger_evaluation(session_id)

    @pytest.mark.asyncio
    async def test_trigger_refuses_for_a_non_sealed_session(self):
        harness = _Harness()
        await harness.seed_shortlisted_application()
        session_id, _ = await harness.interview_service.create_session(
            job_description=_job(), parsed_resume=_resume(), candidate_id="cand_eval",
            application_id="app_eval", max_questions=5,
        )
        with pytest.raises(ConflictError):
            await harness.evaluation_service.trigger_evaluation(session_id)

    @pytest.mark.asyncio
    async def test_trigger_for_unknown_session_is_not_found(self):
        harness = _Harness()
        with pytest.raises(NotFoundError):
            await harness.evaluation_service.trigger_evaluation("never-existed")


# ---------------------------------------------------------------------------
# 15. Evaluation persistence failure
# ---------------------------------------------------------------------------

class TestEvaluationPersistenceFailure:
    @pytest.mark.asyncio
    async def test_a_repository_save_failure_during_run_does_not_crash_the_task(self):
        harness = _Harness()
        await harness.seed_shortlisted_application()
        session_id = await harness.seed_sealed_session(application_id="app_eval")
        job = await harness.evaluation_service.trigger_evaluation(session_id)

        real_save = harness.evaluations.save
        calls = {"n": 0}

        async def flaky_save(record):
            calls["n"] += 1
            if calls["n"] == 2:  # the RUNNING transition
                raise RuntimeError("evaluation store unreachable")
            return await real_save(record)

        harness.evaluations.save = flaky_save  # type: ignore[assignment]

        # Must not raise even though a save() failed partway through.
        await harness.dispatcher.run_all()


# ---------------------------------------------------------------------------
# 16. Background execution: the HTTP request never blocks on the pipeline
# ---------------------------------------------------------------------------

class TestBackgroundExecutionIsReal:
    def test_finish_returns_pending_and_completes_shortly_after(self, client, app):
        """Uses the REAL AsyncTaskEvaluationDispatcher (the app's default) -
        proves the request returns before the pipeline finishes."""
        app.state.interviewer_factory = _scripted_interviewer("Q1?")
        candidate_id, job_id = "cand_http_eval", "job_http_eval"

        job_payload = {
            "job_id": job_id, "title": "Backend Engineer", "description": "Test role",
            "competencies": [{"name": "Python", "weight": 1.0}],
        }
        resume_payload = {"candidate_id": candidate_id, "candidate_name": "Test Candidate",
                          "skills": ["Python"]}

        # Seed an already-shortlisted application directly through the
        # container's repository, exactly as a real ApplicationService run
        # would leave it (bypassing the LLM-driven job/candidate creation
        # endpoints here, which is not what this test is about).
        import asyncio as _asyncio

        async def _seed():
            await app.state.container.application_repository.save(Application(
                application_id="app_http_eval", job_id=job_id, candidate_id=candidate_id,
                status=ApplicationStatus.SHORTLISTED,
                matching_score=_matching_score(candidate_id, job_id),
            ))

        _asyncio.run(_seed())

        created = client.post("/sessions", json={
            "candidate_id": candidate_id, "job_id": job_id,
            "job_description": job_payload, "parsed_resume": resume_payload,
            "max_questions": 1, "application_id": "app_http_eval",
        })
        assert created.status_code == 201
        sid = created.json()["session_id"]

        response = client.post(f"/sessions/{sid}/answers", json={"answer_text": "A real answer."})
        body = response.json()
        assert body["status"] == "sealed"
        assert body["transcript_persisted"] is True
        assert body["evaluation_id"] is not None
        # The request returned WITHOUT waiting for the pipeline - status is
        # whatever it was at trigger time, PENDING or (if the scheduled
        # task happened to get a scheduling slot before the response was
        # built) already RUNNING - never asserted as COMPLETED here.
        assert body["evaluation_status"] in ("pending", "running")

        evaluation_id = body["evaluation_id"]

        async def _wait_for_completion():
            for _ in range(200):
                job = app.state.container.evaluation_repository
                stored = await job.get(evaluation_id)
                if stored.status.value in ("completed", "failed"):
                    return stored
                await _asyncio.sleep(0.02)
            raise AssertionError("evaluation did not finish in time")

        stored = _asyncio.run(_wait_for_completion())
        assert stored.status == EvaluationStatus.COMPLETED
        assert stored.result is not None


# ---------------------------------------------------------------------------
# 17. Evaluation APIs
# ---------------------------------------------------------------------------

class TestEvaluationApi:
    def _seed_and_run(self, app):
        """Synchronous helper for HTTP-layer tests: swaps in a
        RecordingDispatcher, seeds a sealed+application-linked session
        through the container's own repositories, triggers, and runs the
        evaluation to completion - all via the SAME container the
        TestClient uses."""
        import asyncio as _asyncio

        dispatcher = RecordingDispatcher()
        app.state.container.evaluation_dispatcher = dispatcher

        async def _run():
            harness_service = EvaluationService(
                evaluation_repository=app.state.container.evaluation_repository,
                session_repository=app.state.container.session_repository,
                transcript_repository=app.state.container.transcript_repository,
                application_repository=app.state.container.application_repository,
                dispatcher=dispatcher,
            )
            interview_service = InterviewService(
                registry=app.state.registry,
                session_repository=app.state.container.session_repository,
                transcript_repository=app.state.container.transcript_repository,
                interviewer_factory=_scripted_interviewer("Q1?"),
            )
            await app.state.container.application_repository.save(Application(
                application_id="app_api_eval", job_id="job_api_eval", candidate_id="cand_api_eval",
                status=ApplicationStatus.SHORTLISTED,
                matching_score=_matching_score("cand_api_eval", "job_api_eval"),
            ))
            session_id, runner = await interview_service.create_session(
                job_description=_job(job_id="job_api_eval"), parsed_resume=_resume("cand_api_eval"),
                candidate_id="cand_api_eval", max_questions=1, application_id="app_api_eval",
            )
            while not runner.is_finished():
                result = await interview_service.submit_answer(session_id, "A detailed answer.")
                if result.next_question is None:
                    break
            job = await harness_service.trigger_evaluation(session_id)
            await dispatcher.run_all()
            return session_id, job.evaluation_id

        return _asyncio.run(_run())

    def test_get_evaluation_by_id_returns_the_completed_result(self, client, app):
        session_id, evaluation_id = self._seed_and_run(app)
        response = client.get(f"/evaluations/{evaluation_id}")
        assert response.status_code == 200
        body = response.json()["evaluation"]
        assert body["status"] == "completed"
        assert body["result"]["candidate_id"] == "cand_api_eval"

    def test_get_unknown_evaluation_is_404(self, client):
        response = client.get("/evaluations/never-existed")
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    def test_get_session_evaluation_returns_null_before_triggered(self, client, app):
        app.state.interviewer_factory = _scripted_interviewer("Q1?", "Q2?")
        created = client.post("/sessions", json={
            "candidate_id": "cand_pending_eval", "job_id": "job_pending_eval",
            "job_description": {
                "job_id": "job_pending_eval", "title": "T", "description": "D",
                "competencies": [{"name": "Python", "weight": 1.0}],
            },
            "parsed_resume": {"candidate_id": "cand_pending_eval", "candidate_name": "N",
                              "skills": ["Python"]},
            "max_questions": 5,
        })
        sid = created.json()["session_id"]
        response = client.get(f"/sessions/{sid}/evaluation")
        assert response.status_code == 200
        assert response.json()["evaluation"] is None

    def test_get_session_evaluation_returns_the_job_once_triggered(self, client, app):
        session_id, evaluation_id = self._seed_and_run(app)
        response = client.get(f"/sessions/{session_id}/evaluation")
        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == session_id
        assert body["evaluation"]["evaluation_id"] == evaluation_id
        assert body["evaluation"]["status"] == "completed"

    def test_get_evaluation_for_unknown_session_is_404(self, client):
        response = client.get("/sessions/never-existed/evaluation")
        assert response.status_code == 404

    def test_retry_endpoint_refuses_a_completed_evaluation(self, client, app):
        _session_id, evaluation_id = self._seed_and_run(app)
        response = client.post(f"/evaluations/{evaluation_id}/retry")
        assert response.status_code == 409
        assert response.json()["error"] == "conflict"


# ---------------------------------------------------------------------------
# 19. Authorization boundary (inert while AUTH_ENABLED=false, as elsewhere)
# ---------------------------------------------------------------------------

class TestAuthorizationBoundary:
    def test_evaluation_endpoints_work_under_the_default_anonymous_principal(self, client):
        """require_authenticated is attached to every evaluation route,
        exactly like every other route in this backend - inert while
        AUTH_ENABLED=false. Role-based redaction of a completed report is a
        deferred product decision blocked on the identity system (see
        api/models_evaluations.py's docstring), not something enforced or
        silently assumed safe here."""
        response = client.get("/evaluations/never-existed")
        # Reaches the NotFoundError path (not a 401/403), proving the
        # request was authenticated as the anonymous principal and
        # authorized to attempt the call, per the existing Chunk 1 model.
        assert response.status_code == 404
