"""
Chunk 1: backend foundation and persistence boundary.

Covers the plumbing introduced in this chunk - configuration, lifecycle,
middleware, error translation, the repository boundary and the auth
boundary - and, just as importantly, pins the guarantees that make those
safe to build on:

  * a stub persistence layer announces itself and cannot reach production;
  * an unsealed transcript can never be stored;
  * a 422 never echoes the value that failed validation;
  * an internal exception message never reaches a client;
  * "authentication is enabled" is never a claim the service cannot honour;
  * every pre-existing wiring seam still works.

No test here calls a real provider. The interview engine is exercised only
through its existing public API with a scripted fake.
"""
import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.interviewer.agent import InterviewerAgent
from api.app import create_app
from api.registry import SessionRegistry
from core.config import AppSettings
from core.container import ServiceContainer, build_default_container
from core.errors import (
    AppError,
    ConfigurationError,
    ConflictError,
    DependencyError,
    NotFoundError,
    NotImplementedYetError,
)
from core.context import REQUEST_ID_HEADER
from core.lifespan import validate_startup_configuration
from core.middleware import _clean_request_id
from core.security import (
    ANONYMOUS,
    AnonymousAuthProvider,
    Principal,
    PrincipalType,
)
from repositories.interfaces import RepositoryError, SessionRecord
from repositories.memory import (
    InMemoryApplicationRepository,
    InMemoryCandidateRepository,
    InMemoryEvaluationRepository,
    InMemoryJobRepository,
    InMemorySessionRepository,
    InMemoryTranscriptRepository,
    InMemoryUserRepository,
)
from services.evaluation_dispatcher import AsyncTaskEvaluationDispatcher
from schemas.interview import InterviewTranscript
from services.interview_service import InterviewService
from tests.fakes import ScriptedLLMProvider
from utils.interview_session import InterviewSessionRunner, SessionStatus


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_api.py's payload builders)
# ---------------------------------------------------------------------------

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


def _job_payload(job_id="job_found"):
    return {
        "job_id": job_id, "title": "Backend Engineer", "description": "Test role",
        "competencies": [{"name": "Python", "weight": 0.5}, {"name": "SQL", "weight": 0.5}],
    }


def _resume_payload(candidate_id="cand_found"):
    return {"candidate_id": candidate_id, "candidate_name": "Test Candidate",
            "skills": ["Python", "SQL"]}


def _create_payload(candidate_id="cand_found", job_id="job_found"):
    return {
        "candidate_id": candidate_id, "job_id": job_id,
        "job_description": _job_payload(job_id), "parsed_resume": _resume_payload(candidate_id),
    }


def _scripted_interviewer(*question_texts):
    script = [_intro_json()]
    for text in question_texts:
        script.extend([_question_json(text), _eval_json()])
    return lambda: InterviewerAgent(llm_provider=ScriptedLLMProvider(script=script))


@pytest.fixture
def app():
    """A fresh application per test, built through the factory."""
    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class TestAppSettings:
    def test_defaults_are_safe_and_leave_existing_routes_unprefixed(self):
        settings = AppSettings()
        # An api_prefix default of anything but "" would move /sessions and
        # break the frontend and the shipped voice client.
        assert settings.api_prefix == ""
        assert settings.cors_allow_origins == []  # CORS is opt-in, never default-open
        assert settings.auth_enabled is False
        assert settings.debug is False

    def test_api_prefix_is_normalised(self):
        assert AppSettings(api_prefix="api/v1").api_prefix == "/api/v1"
        assert AppSettings(api_prefix="/api/v1/").api_prefix == "/api/v1"
        assert AppSettings(api_prefix="  ").api_prefix == ""

    def test_wildcard_origin_with_credentials_is_rejected(self):
        """The browser rejects this combination, so accepting it here would
        mean shipping cross-origin auth that silently never works."""
        with pytest.raises(ValueError, match="cannot be combined"):
            AppSettings(cors_allow_origins=["*"], cors_allow_credentials=True)

    def test_production_rejects_wildcard_origin_and_debug(self):
        with pytest.raises(ValueError, match="not permitted when ENVIRONMENT=production"):
            AppSettings(environment="production", cors_allow_origins=["*"])
        with pytest.raises(ValueError, match="DEBUG=true is not permitted"):
            AppSettings(environment="production", debug=True)

    def test_invalid_log_level_is_rejected(self):
        with pytest.raises(ValueError, match="LOG_LEVEL must be one of"):
            AppSettings(log_level="CHATTY")

    def test_settings_are_frozen(self):
        settings = AppSettings()
        with pytest.raises(Exception):
            settings.auth_enabled = True  # type: ignore[misc]

    def test_docs_are_hidden_in_production_only(self):
        assert AppSettings().docs_url == "/docs"
        assert AppSettings(environment="production").docs_url is None
        assert AppSettings(environment="production").openapi_url is None

    def test_provider_values_are_read_through_not_copied(self, monkeypatch):
        """The read-through property is what keeps config/settings.py the
        single source of truth - and what keeps the existing
        `monkeypatch.setattr("config.settings....")` tests meaningful."""
        settings = AppSettings()
        monkeypatch.setattr("config.settings.LLM_PROVIDER", "sentinel-provider")
        assert settings.llm_provider == "sentinel-provider"

    def test_public_summary_carries_no_credential(self, monkeypatch):
        monkeypatch.setattr("config.settings.GROQ_API_KEY", "sk-secret-value")
        monkeypatch.setattr("config.settings.AZURE_SPEECH_KEY", "azure-secret-value")
        blob = json.dumps(AppSettings().public_summary())
        assert "sk-secret-value" not in blob
        assert "azure-secret-value" not in blob


# ---------------------------------------------------------------------------
# Lifecycle / startup validation
# ---------------------------------------------------------------------------

class TestStartupValidation:
    def _container(self, settings, **overrides):
        base = dict(
            settings=settings,
            session_repository=InMemorySessionRepository(),
            transcript_repository=InMemoryTranscriptRepository(),
            job_repository=InMemoryJobRepository(),
            candidate_repository=InMemoryCandidateRepository(),
            application_repository=InMemoryApplicationRepository(),
            evaluation_repository=InMemoryEvaluationRepository(),
            user_repository=InMemoryUserRepository(),
            evaluation_dispatcher=AsyncTaskEvaluationDispatcher(),
            auth_provider=AnonymousAuthProvider(),
            persistence_is_ephemeral=True,
        )
        base.update(overrides)
        return ServiceContainer(**base)

    def test_auth_enabled_without_a_real_provider_refuses_to_start(self):
        """'Auth is on' must never be a claim the service cannot back up."""
        settings = AppSettings(auth_enabled=True)
        with pytest.raises(ConfigurationError) as exc:
            validate_startup_configuration(settings, self._container(settings))
        assert "AUTH_ENABLED=true" in exc.value.internal_detail

    def test_production_with_ephemeral_persistence_refuses_to_start(self):
        """The stub repositories losing every transcript on restart is data
        loss, not a degraded mode - it must not be deployable."""
        settings = AppSettings(environment="production")
        with pytest.raises(ConfigurationError) as exc:
            validate_startup_configuration(settings, self._container(settings))
        assert "in-memory persistence" in exc.value.internal_detail

    def test_development_with_ephemeral_persistence_is_allowed(self):
        settings = AppSettings()
        validate_startup_configuration(settings, self._container(settings))  # no raise

    def test_configuration_error_detail_is_not_client_facing(self):
        settings = AppSettings(environment="production")
        with pytest.raises(ConfigurationError) as exc:
            validate_startup_configuration(settings, self._container(settings))
        # The operator-facing explanation lives in internal_detail; the body
        # a client would ever see stays generic.
        assert exc.value.to_body()["detail"] == "The service is not correctly configured."


class TestLifecycle:
    def test_container_is_built_on_startup_and_released_on_shutdown(self, app):
        closed = []

        with TestClient(app):
            container = app.state.container
            assert isinstance(container, ServiceContainer)
            container.session_repository.aclose = lambda: _record(closed)  # type: ignore[attr-defined]

        assert closed == ["closed"]

    def test_shutdown_survives_a_component_that_fails_to_close(self, app):
        """One broken collaborator must not prevent the others from being
        released, or a redeploy leaks whatever they hold."""
        released = []

        async def boom():
            raise RuntimeError("close failed")

        async def fine():
            released.append("transcripts")

        with TestClient(app):
            app.state.container.session_repository.aclose = boom  # type: ignore[attr-defined]
            app.state.container.transcript_repository.aclose = fine  # type: ignore[attr-defined]

        assert released == ["transcripts"]


async def _record(sink):
    sink.append("closed")


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class TestRequestContextMiddleware:
    def test_every_response_carries_a_request_id_and_timing(self, client):
        response = client.get("/health")
        assert response.headers[REQUEST_ID_HEADER]
        assert float(response.headers["X-Response-Time-ms"]) >= 0

    def test_inbound_request_id_is_echoed_so_a_trace_survives_a_gateway(self, client):
        response = client.get("/health", headers={REQUEST_ID_HEADER: "trace-abc-123"})
        assert response.headers[REQUEST_ID_HEADER] == "trace-abc-123"

    def test_request_id_is_sanitised_against_log_injection(self):
        """It lands in every log line for the request, so a newline or an
        unbounded length in the inbound header must not survive."""
        cleaned = _clean_request_id("abc\ninjected WARNING fake-line")
        assert "\n" not in cleaned
        assert " " not in cleaned
        assert len(_clean_request_id("x" * 500)) == 64
        assert _clean_request_id("!!!") != "!!!"  # falls back to a generated id
        assert _clean_request_id(None)

    def test_request_id_appears_in_error_bodies(self, client):
        response = client.get("/sessions/does-not-exist")
        assert response.status_code == 404
        assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]

    def test_concurrent_requests_do_not_share_a_request_id(self, app):
        """The id is a ContextVar, not a global - two in-flight interview
        requests must never be logged under each other's id."""
        import httpx

        async def run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                responses = await asyncio.gather(*(ac.get("/health") for _ in range(8)))
            return [r.headers[REQUEST_ID_HEADER] for r in responses]

        ids = asyncio.run(run())
        assert len(set(ids)) == len(ids)


class TestBodySizeLimit:
    def test_oversized_body_is_rejected_at_the_edge(self):
        app = create_app(AppSettings(max_request_body_bytes=2048))
        with TestClient(app) as client:
            response = client.post("/sessions", content=b"x" * 4096,
                                   headers={"content-type": "application/json"})
        assert response.status_code == 413
        assert response.json()["error"] == "payload_too_large"

    def test_normal_body_is_unaffected(self, client):
        # Proves the limit does not interfere with an ordinary request.
        assert client.get("/health").status_code == 200


class TestCors:
    def test_cors_is_absent_unless_configured(self, client):
        response = client.get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in response.headers

    def test_configured_origin_is_allowed_and_others_are_not(self):
        app = create_app(AppSettings(cors_allow_origins=["https://hire.example"]))
        with TestClient(app) as client:
            allowed = client.get("/health", headers={"Origin": "https://hire.example"})
            assert allowed.headers["access-control-allow-origin"] == "https://hire.example"

            denied = client.get("/health", headers={"Origin": "https://evil.example"})
            assert "access-control-allow-origin" not in denied.headers

    def test_request_id_header_is_readable_cross_origin(self):
        """Without expose_headers a browser cannot read the id off a
        cross-origin response, so a frontend can never report it in a bug."""
        app = create_app(AppSettings(cors_allow_origins=["https://hire.example"]))
        with TestClient(app) as client:
            response = client.get("/health", headers={"Origin": "https://hire.example"})
        assert REQUEST_ID_HEADER in response.headers["access-control-expose-headers"]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def _app_raising(self, exc: Exception) -> FastAPI:
        app = create_app()

        @app.get("/boom")
        async def boom():
            raise exc

        return app

    @pytest.mark.parametrize(
        "error,expected_status,expected_code",
        [
            (NotFoundError(), 404, "not_found"),
            (ConflictError(), 409, "conflict"),
            (DependencyError(), 503, "dependency_unavailable"),
            (NotImplementedYetError(), 501, "not_implemented"),
        ],
    )
    def test_typed_errors_map_to_their_status(self, error, expected_status, expected_code):
        with TestClient(self._app_raising(error)) as client:
            response = client.get("/boom")
        assert response.status_code == expected_status
        assert response.json()["error"] == expected_code

    def test_internal_detail_is_never_returned_to_the_client(self):
        error = NotFoundError(
            internal_detail="postgres://user:hunter2@db.internal/openhire row 42 missing"
        )
        with TestClient(self._app_raising(error)) as client:
            response = client.get("/boom")
        assert "hunter2" not in response.text
        assert "db.internal" not in response.text
        assert response.json()["detail"] == "The requested resource was not found."

    def test_unexpected_exception_leaks_nothing(self):
        with TestClient(self._app_raising(RuntimeError("/srv/secret/path exploded")),
                        raise_server_exceptions=False) as client:
            response = client.get("/boom")
        assert response.status_code == 500
        assert "/srv/secret/path" not in response.text
        assert response.json()["error"] == "internal_error"

    def test_validation_error_has_the_standard_shape(self, client):
        response = client.post("/sessions", json={"candidate_id": "c"})
        assert response.status_code == 422
        body = response.json()
        assert body["error"] == "validation_error"
        assert isinstance(body["errors"], list)
        assert all({"field", "message", "type"} == set(e) for e in body["errors"])

    def test_validation_error_does_not_echo_the_rejected_value(self, client):
        """pydantic's own error payload includes the offending input. For
        this API that input can be an entire answer or resume, so echoing it
        would put candidate content into a client-side error log."""
        secret = "MY-CANDIDATE-SSN-123-45-6789"
        response = client.post("/sessions", json={"candidate_id": secret, "job_id": 5})
        assert response.status_code == 422
        assert secret not in response.text

    def test_existing_error_shapes_are_unchanged(self, client):
        """The pre-existing contracts other tests and clients depend on."""
        not_found = client.get("/sessions/nope")
        assert not_found.status_code == 404
        assert not_found.json()["error"] == "session_not_found"

        payload = _create_payload()
        payload["candidate_id"] = "mismatched"
        bad = client.post("/sessions", json=payload)
        assert bad.status_code == 400
        assert bad.json()["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# Persistence boundary
# ---------------------------------------------------------------------------

class TestSessionRepository:
    @pytest.mark.asyncio
    async def test_save_is_idempotent_and_preserves_creation_time(self):
        repo = InMemorySessionRepository()
        first = await repo.save(SessionRecord(session_id="s1", candidate_id="c1",
                                              job_id="j1", status=SessionStatus.ACTIVE))
        second = await repo.save(SessionRecord(session_id="s1", candidate_id="c1",
                                               job_id="j1", status=SessionStatus.SEALED))
        assert second.created_at == first.created_at
        assert second.updated_at >= first.updated_at
        assert second.status == SessionStatus.SEALED

    @pytest.mark.asyncio
    async def test_absence_returns_none_rather_than_raising(self):
        """Absence is a normal result. Making callers handle it explicitly is
        what stops a missing record being treated as an empty one."""
        assert await InMemorySessionRepository().get("never-existed") is None

    @pytest.mark.asyncio
    async def test_listing_is_scoped_to_one_candidate(self):
        repo = InMemorySessionRepository()
        await repo.save(SessionRecord(session_id="s1", candidate_id="c1", job_id="j1",
                                      status=SessionStatus.SEALED))
        await repo.save(SessionRecord(session_id="s2", candidate_id="c2", job_id="j1",
                                      status=SessionStatus.SEALED))
        found = await repo.list_for_candidate("c1")
        assert [r.session_id for r in found] == ["s1"]

    @pytest.mark.asyncio
    async def test_delete_reports_whether_a_record_existed(self):
        repo = InMemorySessionRepository()
        await repo.save(SessionRecord(session_id="s1", candidate_id="c1", job_id="j1",
                                      status=SessionStatus.ACTIVE))
        assert await repo.delete("s1") is True
        assert await repo.delete("s1") is False


class TestTranscriptRepository:
    def _sealed(self, sealed=True, interview_id="int_1"):
        return InterviewTranscript(
            interview_id=interview_id, candidate_id="c1", job_id="j1",
            exchanges=[], start_time="2024-01-15T10:00:00Z", is_sealed=sealed,
        )

    @pytest.mark.asyncio
    async def test_unsealed_transcript_is_refused(self):
        """Storing a partial transcript is how a half-finished interview
        gets scored as if it were complete."""
        with pytest.raises(RepositoryError, match="unsealed"):
            await InMemoryTranscriptRepository().save(self._sealed(sealed=False))

    @pytest.mark.asyncio
    async def test_sealed_transcript_round_trips(self):
        repo = InMemoryTranscriptRepository()
        await repo.save(self._sealed())
        stored = await repo.get("int_1")
        assert stored is not None and stored.is_sealed

    @pytest.mark.asyncio
    async def test_stored_transcript_is_isolated_from_later_caller_mutation(self):
        """A real database serialises on write; the stub must not be more
        permissive than the thing it stands in for."""
        repo = InMemoryTranscriptRepository()
        transcript = self._sealed()
        await repo.save(transcript)
        transcript.candidate_id = "tampered"
        assert (await repo.get("int_1")).candidate_id == "c1"

    @pytest.mark.asyncio
    async def test_lookup_requires_both_candidate_and_job(self):
        repo = InMemoryTranscriptRepository()
        await repo.save(self._sealed())
        assert len(await repo.get_for_candidate("c1", "j1")) == 1
        assert await repo.get_for_candidate("c1", "other-job") == []


class TestSessionRecordProjection:
    def test_a_created_session_projects_without_inventing_state(self, sample_job_description,
                                                                sample_parsed_resume):
        """A session can legitimately be recorded before start(), when it has
        no InterviewState at all."""
        runner = InterviewSessionRunner(sample_job_description, sample_parsed_resume)
        record = SessionRecord.from_runner("s1", runner)
        assert record.status == SessionStatus.CREATED
        assert record.questions_asked == 0
        assert record.candidate_id == sample_parsed_resume.candidate_id
        assert record.job_id == sample_job_description.job_id

    def test_runner_accessors_are_read_only(self, sample_job_description, sample_parsed_resume):
        runner = InterviewSessionRunner(sample_job_description, sample_parsed_resume)
        assert runner.candidate_id == sample_parsed_resume.candidate_id
        assert runner.interview_id.startswith("int_")
        with pytest.raises(AttributeError):
            runner.candidate_id = "someone-else"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------

class TestInterviewServicePersistence:
    def _service(self, registry=None):
        registry = registry or SessionRegistry()
        return InterviewService(
            registry=registry,
            session_repository=InMemorySessionRepository(),
            transcript_repository=InMemoryTranscriptRepository(),
            interviewer_factory=_scripted_interviewer("Q1?", "Q2?"),
        )

    @pytest.mark.asyncio
    async def test_creating_a_session_records_it(self, sample_job_description,
                                                 sample_parsed_resume):
        service = self._service()
        session_id, runner = await service.create_session(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            candidate_id=sample_parsed_resume.candidate_id,
        )
        record = await service.get_record(session_id)
        assert record.session_id == session_id
        assert record.status == runner.status

    @pytest.mark.asyncio
    async def test_sealed_interview_transcript_is_persisted(self, sample_job_description,
                                                            sample_parsed_resume):
        """The gap this boundary exists to close: before it, a completed
        interview's transcript lived only inside a process-local object."""
        service = self._service()
        session_id, runner = await service.create_session(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            candidate_id=sample_parsed_resume.candidate_id,
            max_questions=2,
        )
        while not runner.is_finished():
            result = await service.submit_answer(session_id, "A detailed answer.")
            if result.next_question is None:
                break

        assert runner.status == SessionStatus.SEALED
        stored = await service._transcripts.get(runner.interview_id)
        assert stored is not None
        assert stored.is_sealed is True
        assert len(stored.exchanges) == runner.get_state().questions_answered + 1

    @pytest.mark.asyncio
    async def test_a_persistence_failure_does_not_fail_a_live_interview(
        self, sample_job_description, sample_parsed_resume
    ):
        """A candidate cannot act on a storage fault, and their answer has
        already been accepted by the engine - failing the request would
        discard real work."""
        service = self._service()

        session_id, runner = await service.create_session(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            candidate_id=sample_parsed_resume.candidate_id,
        )
        async def broken_save(record):
            raise RuntimeError("database is on fire")

        service._sessions.save = broken_save  # type: ignore[assignment]
        await _answer_intro(service, session_id)
        result = await service.submit_answer(session_id, "A detailed answer.")
        assert result.answer.answer_text == "A detailed answer."
        assert runner.get_state().questions_answered == 1

    @pytest.mark.asyncio
    async def test_get_record_for_an_unknown_session_raises_not_found(self):
        with pytest.raises(NotFoundError):
            await self._service().get_record("never-existed")


# ---------------------------------------------------------------------------
# Sealed transcript persistence correction
#
# A failed transcript write must never be reported as a success, and a
# subsequent read must be able to retry it - see
# services/interview_service.py's module docstring.
# ---------------------------------------------------------------------------

async def _drive_to_sealed(service, sample_job_description, sample_parsed_resume, *, max_questions=2):
    session_id, runner = await service.create_session(
        job_description=sample_job_description,
        parsed_resume=sample_parsed_resume,
        candidate_id=sample_parsed_resume.candidate_id,
        max_questions=max_questions,
    )
    while not runner.is_finished():
        result = await service.submit_answer(session_id, "A detailed answer.")
        if result.next_question is None:
            break
    assert runner.status == SessionStatus.SEALED
    return session_id, runner


async def _answer_intro(service, session_id, text="Hi, nice to meet you."):
    return await service.submit_answer(session_id, text)


def _break_transcript_save(service):
    async def broken_save(transcript):
        raise RuntimeError("transcript store is unreachable")

    service._transcripts.save = broken_save  # type: ignore[assignment]


class TestSealedTranscriptPersistenceCorrection:
    def _service(self):
        return InterviewService(
            registry=SessionRegistry(),
            session_repository=InMemorySessionRepository(),
            transcript_repository=InMemoryTranscriptRepository(),
            interviewer_factory=_scripted_interviewer("Q1?", "Q2?"),
        )

    # -- service layer ------------------------------------------------

    @pytest.mark.asyncio
    async def test_successful_persistence_is_reported_true(
        self, sample_job_description, sample_parsed_resume
    ):
        service = self._service()
        session_id, runner = await _drive_to_sealed(
            service, sample_job_description, sample_parsed_resume
        )
        assert await service.get_transcript_persistence_status(runner) is True
        assert (await service._transcripts.get(runner.interview_id)) is not None

    @pytest.mark.asyncio
    async def test_persistence_failure_is_reported_false_not_true(
        self, sample_job_description, sample_parsed_resume
    ):
        """The exact defect being corrected: a failed transcript write must
        never look identical to a successful one."""
        service = self._service()
        session_id, runner = await service.create_session(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            candidate_id=sample_parsed_resume.candidate_id,
            max_questions=1,
        )
        _break_transcript_save(service)

        await _answer_intro(service, session_id)
        result = await service.submit_answer(session_id, "A detailed answer.")
        assert result.session_status == SessionStatus.SEALED

        # The interview itself completed via the engine's own rules, but
        # persistence must be explicitly marked failed, never true.
        assert await service.get_transcript_persistence_status(runner) is False
        assert (await service._transcripts.get(runner.interview_id)) is None

    @pytest.mark.asyncio
    async def test_session_completion_is_unaffected_by_a_transcript_write_failure(
        self, sample_job_description, sample_parsed_resume
    ):
        """Requirement: InterviewSessionRunner's own lifecycle rules are
        untouched - the interview still seals even though its transcript
        could not be stored."""
        service = self._service()
        session_id, runner = await service.create_session(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            candidate_id=sample_parsed_resume.candidate_id,
            max_questions=1,
        )
        _break_transcript_save(service)

        await _answer_intro(service, session_id)
        result = await service.submit_answer(session_id, "A detailed answer.")
        assert result.session_status == SessionStatus.SEALED
        assert runner.status == SessionStatus.SEALED
        assert runner.get_transcript().is_sealed is True

    @pytest.mark.asyncio
    async def test_not_yet_sealed_reports_none_not_false(
        self, sample_job_description, sample_parsed_resume
    ):
        """None means "not applicable" (nothing to persist yet) - it must
        never be conflated with False ("persistence failed")."""
        service = self._service()
        session_id, runner = await service.create_session(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            candidate_id=sample_parsed_resume.candidate_id,
        )
        assert await service.get_transcript_persistence_status(runner) is None
        assert await service.retry_transcript_persistence(session_id, runner) is None

    @pytest.mark.asyncio
    async def test_retry_recovers_after_the_repository_comes_back(
        self, sample_job_description, sample_parsed_resume
    ):
        """The client-retry path this correction adds: a client rereading
        session state after a storage failure is what triggers the retry -
        no separate worker required."""
        service = self._service()
        session_id, runner = await service.create_session(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            candidate_id=sample_parsed_resume.candidate_id,
            max_questions=1,
        )
        _break_transcript_save(service)
        await _answer_intro(service, session_id)
        await service.submit_answer(session_id, "A detailed answer.")
        assert await service.get_transcript_persistence_status(runner) is False

        # The repository "recovers" - restore the real save method.
        service._transcripts.save = InMemoryTranscriptRepository.save.__get__(
            service._transcripts
        )
        recovered = await service.retry_transcript_persistence(session_id, runner)
        assert recovered is True
        assert await service.get_transcript_persistence_status(runner) is True

    @pytest.mark.asyncio
    async def test_retry_is_idempotent_and_never_duplicates_the_stored_transcript(
        self, sample_job_description, sample_parsed_resume
    ):
        """Requirement 9: safe for a client to retry. Repeated retries after
        success must remain no-ops - never a second write, never a
        duplicate record."""
        service = self._service()
        session_id, runner = await _drive_to_sealed(
            service, sample_job_description, sample_parsed_resume
        )
        assert await service.get_transcript_persistence_status(runner) is True

        calls = []
        original_save = service._transcripts.save

        async def counting_save(transcript):
            calls.append(transcript.interview_id)
            return await original_save(transcript)

        service._transcripts.save = counting_save  # type: ignore[assignment]

        for _ in range(3):
            assert await service.retry_transcript_persistence(session_id, runner) is True

        # Already stored - retry_transcript_persistence short-circuits
        # without writing at all.
        assert calls == []
        stored = await service._transcripts.get(runner.interview_id)
        assert stored is not None and stored.interview_id == runner.interview_id

    @pytest.mark.asyncio
    async def test_repository_upsert_is_itself_safe_to_call_twice(self):
        """Even a direct, repeated save() (bypassing the short-circuit
        above) must upsert rather than duplicate - the property the service
        layer's idempotency relies on."""
        from schemas.interview import InterviewTranscript

        repo = InMemoryTranscriptRepository()
        transcript = InterviewTranscript(
            interview_id="int_retry_1", candidate_id="c1", job_id="j1",
            exchanges=[], start_time="2024-01-15T10:00:00Z", is_sealed=True,
        )
        await repo.save(transcript)
        await repo.save(transcript)
        assert (await repo.get_for_candidate("c1", "j1")) == [
            (await repo.get("int_retry_1"))
        ]

    # -- HTTP layer -----------------------------------------------------

    def _job(self, job_id="job_persist"):
        return _job_payload(job_id)

    def _resume(self, candidate_id="cand_persist"):
        return _resume_payload(candidate_id)

    def test_submit_answer_reports_transcript_persisted_true_on_success(self, app, client):
        app.state.interviewer_factory = _scripted_interviewer("Q1?")
        created = client.post(
            "/sessions",
            json={
                "candidate_id": "cand_persist", "job_id": "job_persist",
                "job_description": self._job(), "parsed_resume": self._resume(),
                "max_questions": 1,
            },
        ).json()
        sid = created["session_id"]

        intro = client.post(f"/sessions/{sid}/answers", json={"answer_text": "Hi, nice to meet you."})
        assert intro.status_code == 200
        response = client.post(f"/sessions/{sid}/answers", json={"answer_text": "A detailed answer."})
        body = response.json()
        assert body["status"] == "sealed"
        assert body["transcript_persisted"] is True

    def test_submit_answer_reports_transcript_persisted_false_on_failure_not_true(
        self, app, client
    ):
        """No false success at the HTTP boundary: the client must be able to
        tell a failed write from a successful one in the same response that
        reports the interview as sealed."""
        app.state.interviewer_factory = _scripted_interviewer("Q1?")
        created = client.post(
            "/sessions",
            json={
                "candidate_id": "cand_persist_fail", "job_id": "job_persist",
                "job_description": self._job(), "parsed_resume": self._resume("cand_persist_fail"),
                "max_questions": 1,
            },
        ).json()
        sid = created["session_id"]

        async def broken_save(transcript):
            raise RuntimeError("transcript store is unreachable")

        app.state.container.transcript_repository.save = broken_save  # type: ignore[attr-defined]

        intro = client.post(f"/sessions/{sid}/answers", json={"answer_text": "Hi, nice to meet you."})
        assert intro.status_code == 200
        response = client.post(f"/sessions/{sid}/answers", json={"answer_text": "A detailed answer."})
        body = response.json()
        # The interview still completed successfully (the engine's own
        # rules are untouched) ...
        assert response.status_code == 200
        assert body["status"] == "sealed"
        # ... but persistence is explicitly reported as failed, never as
        # a bare, indistinguishable "sealed" success.
        assert body["transcript_persisted"] is False

    def test_reading_session_state_retries_and_recovers_a_failed_persistence(
        self, app, client
    ):
        """The idempotent retry path: the client does nothing special, it
        just re-reads the session (as it would to check status), and the
        failed write is retried and succeeds once the repository recovers."""
        app.state.interviewer_factory = _scripted_interviewer("Q1?")
        created = client.post(
            "/sessions",
            json={
                "candidate_id": "cand_persist_retry", "job_id": "job_persist",
                "job_description": self._job(), "parsed_resume": self._resume("cand_persist_retry"),
                "max_questions": 1,
            },
        ).json()
        sid = created["session_id"]

        async def broken_save(transcript):
            raise RuntimeError("transcript store is unreachable")

        repo = app.state.container.transcript_repository
        repo.save = broken_save  # type: ignore[attr-defined]

        intro = client.post(f"/sessions/{sid}/answers", json={"answer_text": "Hi, nice to meet you."})
        assert intro.status_code == 200
        failed = client.post(f"/sessions/{sid}/answers", json={"answer_text": "A detailed answer."})
        assert failed.json()["transcript_persisted"] is False

        # Repository recovers.
        repo.save = InMemoryTranscriptRepository.save.__get__(repo)

        retried = client.get(f"/sessions/{sid}")
        assert retried.json()["transcript_persisted"] is True

        # And a further read does not re-attempt a write at all.
        calls = []
        original = repo.save

        async def counting_save(transcript):
            calls.append(transcript.interview_id)
            return await original(transcript)

        repo.save = counting_save  # type: ignore[attr-defined]
        again = client.get(f"/sessions/{sid}")
        assert again.json()["transcript_persisted"] is True
        assert calls == []

    def test_transcript_persisted_is_none_before_the_session_seals(self, app, client):
        app.state.interviewer_factory = _scripted_interviewer("Q1?", "Q2?")
        created = client.post(
            "/sessions",
            json={
                "candidate_id": "cand_persist_early", "job_id": "job_persist",
                "job_description": self._job(), "parsed_resume": self._resume("cand_persist_early"),
                "max_questions": 5,
            },
        ).json()
        sid = created["session_id"]

        state = client.get(f"/sessions/{sid}").json()
        assert state["status"] != "sealed"
        assert state["transcript_persisted"] is None


# ---------------------------------------------------------------------------
# Dependency container
# ---------------------------------------------------------------------------

class TestContainer:
    def test_registry_is_read_through_so_the_existing_seam_still_works(self):
        """tests/test_api.py replaces app.state.registry between requests;
        the container must never cache a stale one."""
        container = build_default_container(AppSettings())

        class _App:
            class state:
                registry = None

        app = _App()
        first, second = SessionRegistry(), SessionRegistry()
        app.state.registry = first
        assert container.registry_for(app) is first
        app.state.registry = second
        assert container.registry_for(app) is second

    def test_factory_seams_are_read_through(self):
        container = build_default_container(AppSettings())

        class _App:
            class state:
                interviewer_factory = None
                voice_service_factory = None

        app = _App()
        assert container.interviewer_factory_for(app) is None
        marker = object()
        app.state.interviewer_factory = marker
        assert container.interviewer_factory_for(app) is marker

    def test_stub_persistence_is_flagged(self):
        assert build_default_container(AppSettings()).persistence_is_ephemeral is True

    def test_root_endpoint_announces_ephemeral_persistence(self, client):
        """A deployment on the stubs must not look healthy while silently
        losing every transcript on restart."""
        body = client.get("/").json()
        assert body["persistence"] == "ephemeral"

    def test_root_endpoint_returns_no_credential(self, client, monkeypatch):
        monkeypatch.setattr("config.settings.GROQ_API_KEY", "sk-live-do-not-leak")
        assert "sk-live-do-not-leak" not in client.get("/").text


# ---------------------------------------------------------------------------
# Auth boundary
# ---------------------------------------------------------------------------

class TestAuthBoundary:
    def test_principal_defaults_to_anonymous_and_is_frozen(self):
        assert ANONYMOUS.principal_type is PrincipalType.ANONYMOUS
        assert ANONYMOUS.is_authenticated is False
        with pytest.raises(Exception):
            ANONYMOUS.scopes = frozenset({"admin"})  # type: ignore[misc]

    def test_scope_checking(self):
        recruiter = Principal(
            principal_type=PrincipalType.RECRUITER,
            subject_id="rec_1",
            scopes=frozenset({"interview:read", "interview:write"}),
        )
        assert recruiter.is_authenticated
        assert recruiter.has_scopes(["interview:read"])
        assert not recruiter.has_scopes(["interview:read", "job:delete"])

    def test_principal_carries_no_credential(self):
        assert "token" not in Principal.model_fields
        assert "password" not in Principal.model_fields

    def test_session_endpoints_are_unchanged_while_auth_is_disabled(self, client):
        """The whole point of adding the dependency now: attaching it must
        not change any endpoint's behaviour today."""
        response = client.post("/sessions", json=_create_payload())
        assert response.status_code == 201

    def test_enabling_auth_without_a_provider_is_a_startup_failure_not_a_silent_noop(self):
        settings = AppSettings(auth_enabled=True)
        app = create_app(settings)
        with pytest.raises(ConfigurationError):
            with TestClient(app):
                pass


# ---------------------------------------------------------------------------
# Regression guard for the layering rules this chunk is built on
# ---------------------------------------------------------------------------

class TestLayering:
    def test_only_the_container_names_the_stub_persistence_module(self):
        """Swapping in the database must be a change to core/container.py and
        nowhere else. If another module imports repositories.memory, that
        promise is already broken."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if parts & {"venv", "__pycache__", ".git", "tests"}:
                continue
            if path.name == "memory.py" or path.parent.name == "repositories":
                continue
            if "repositories.memory" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(root)))
        assert offenders == [str(pathlib.Path("core") / "container.py")], offenders

    def test_the_service_layer_holds_no_interview_intelligence(self):
        """Guards the boundary services/interview_service.py declares: if it
        ever inspects an answer or reasons about a competency, the logic has
        been put in the wrong layer."""
        import pathlib

        source = (pathlib.Path(__file__).resolve().parent.parent
                  / "services" / "interview_service.py").read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines()
            if not line.strip().startswith("#")
        )
        for forbidden in ("decide_next_action", "build_answer_evidence", "generate_next_question",
                          "evaluate_answer", "record_answer"):
            assert forbidden not in code, f"{forbidden} must stay in the interview engine"
