"""Integration tests for Gemini Live HTTP, WebSocket control channel, and lifecycle."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import config.settings
from api.app import create_app
from api.models import CreateSessionRequest
from api.models_live_interview import (
    LiveFinishRequest,
    LiveSessionResponse,
    LiveTokenRequest,
)
from core.config import AppSettings, get_settings
from core.container import ServiceContainer
from core.security import JWTAuthProvider
from repositories.interfaces import CandidateRecord
from repositories.memory import (
    InMemoryApplicationRepository,
    InMemoryAuditLogRepository,
    InMemoryBugReportRepository,
    InMemoryCandidateRepository,
    InMemoryEvaluationRepository,
    InMemoryJobRepository,
    InMemoryLLMCredentialRepository,
    InMemoryRubricRepository,
    InMemorySessionRepository,
    InMemoryTranscriptRepository,
    InMemoryUserRepository,
)
from schemas.live_interview import LiveInterviewStatus
from schemas.resume import ParsedResume
from schemas.rubric import AnchoredCompetency, JobRubric, RubricStatus
from services.auth_service import AuthService
from services.evaluation_dispatcher import AsyncTaskEvaluationDispatcher
from services.gemini_live_token import GeminiLiveTokenService


class FakeAsyncAuthTokens:
    def __init__(self, owner: Any) -> None:
        self.owner = owner

    async def create(self, *, config: Any) -> Any:
        self.owner.last_config = config
        return SimpleNamespace(
            name=self.owner.name,
            expire_time="2026-10-09T00:20:00Z",
        )


class FakeAuthTokenClient:
    def __init__(self, name: str = "fake-ephemeral-gemini-token") -> None:
        self.name = name
        self.last_config: Any = None
        self.aio = SimpleNamespace(auth_tokens=FakeAsyncAuthTokens(self))


def _make_test_token_service(secret: str) -> GeminiLiveTokenService:
    return GeminiLiveTokenService(
        system_api_key="system-fake-api-key",
        model="gemini-live-test",
        control_secret=secret,
        client_factory=lambda api_key: FakeAuthTokenClient(),
    )


def _anchored_rubric(job_id: str) -> JobRubric:
    anchors = {level: f"Level {level}" for level in range(1, 6)}
    return JobRubric(
        rubric_id=f"rub_{job_id}",
        job_id=job_id,
        version=1,
        status=RubricStatus.APPROVED,
        competencies=[
            AnchoredCompetency(
                name="Python",
                definition="Writes clean Python code",
                weight=0.5,
                anchors=anchors,
            ),
            AnchoredCompetency(
                name="System Design",
                definition="Designs scalable distributed systems",
                weight=0.5,
                anchors=anchors,
            ),
        ],
    )


def _build_test_app(settings: AppSettings | None = None):
    app_settings = settings or AppSettings(
        jwt_secret_key="test-jwt-secret-that-is-at-least-thirty-two-bytes-long",
        auth_enabled=False,
    )
    app = create_app(app_settings)
    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: app_settings
    app.state.gemini_live_token_factory = lambda: _make_test_token_service(
        app_settings.jwt_secret_key
    )
    return app


@pytest.fixture(autouse=True)
def enable_gemini_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "GEMINI_LIVE_ENABLED", True)


@pytest.fixture
def test_app():
    app = _build_test_app()
    return app


@pytest.fixture
def client(test_app):
    with TestClient(test_app) as c:
        # Pre-seed approved rubric for test job
        job_id = "job_live_001"
        test_app.state.container.rubric_repository._rubrics[f"rub_{job_id}"] = _anchored_rubric(job_id)
        yield c


def _sample_payload(
    candidate_id: str = "cand_live_001", job_id: str = "job_live_001"
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "job_id": job_id,
        "job_description": {
            "job_id": job_id,
            "title": "Senior Python Engineer",
            "description": "Backend API engineer",
            "experience_years": 4,
            "required_skills": ["Python", "FastAPI"],
            "competencies": [
                {"name": "Python", "weight": 0.5},
                {"name": "System Design", "weight": 0.5},
            ],
        },
        "parsed_resume": {
            "candidate_id": candidate_id,
            "candidate_name": "Alice Candidate",
            "email": "alice@example.com",
            "skills": ["Python", "Docker"],
            "total_experience_years": 5,
        },
    }


def _transcript_event(sequence: int, speaker: str = "candidate", text: str = "Hello", **kwargs) -> dict[str, Any]:
    return {
        "event_id": f"evt-{sequence}",
        "sequence": sequence,
        "speaker": speaker,
        "text": text,
        "started_at_ms": (sequence - 1) * 2000,
        "ended_at_ms": sequence * 2000,
        "interrupted": False,
        **kwargs,
    }


def test_create_live_session_success(client: TestClient) -> None:
    payload = _sample_payload()
    response = client.post("/live-sessions", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["interview_mode"] == "gemini_live"
    assert data["status"] == "created"
    assert data["reconnect_after_seconds"] == 540
    assert "session_id" in data


def test_create_live_session_disabled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "GEMINI_LIVE_ENABLED", False)
    response = client.post("/live-sessions", json=_sample_payload())
    assert response.status_code == 503
    data = response.json()
    assert data["error"] == "gemini_live_disabled"


def test_create_live_session_unconfigured(test_app, monkeypatch: pytest.MonkeyPatch) -> None:
    test_app.state.gemini_live_token_factory = None
    monkeypatch.setattr(config.settings, "GEMINI_API_KEY", "")
    with TestClient(test_app) as client:
        response = client.post("/live-sessions", json=_sample_payload())
        assert response.status_code == 503
        data = response.json()
        assert data["error"] == "gemini_live_unconfigured"


def test_create_demo_live_session(client: TestClient) -> None:
    response = client.post("/live-sessions/demo")
    assert response.status_code == 201
    data = response.json()
    assert data["interview_mode"] == "gemini_live"
    assert data["reconnect_after_seconds"] == 540
    assert "session_id" in data


def test_get_live_session(client: TestClient) -> None:
    create_res = client.post("/live-sessions", json=_sample_payload())
    session_id = create_res.json()["session_id"]

    get_res = client.get(f"/live-sessions/{session_id}")
    assert get_res.status_code == 200
    data = get_res.json()
    assert data["session_id"] == session_id
    assert data["status"] == "created"
    assert data["reconnect_after_seconds"] == 540

    not_found = client.get("/live-sessions/missing-session")
    assert not_found.status_code == 404


def test_token_issuance_never_leaks_permanent_api_key(client: TestClient) -> None:
    create_res = client.post("/live-sessions", json=_sample_payload())
    session_id = create_res.json()["session_id"]

    token_res = client.post(f"/live-sessions/{session_id}/token")
    assert token_res.status_code == 200
    data = token_res.json()
    assert data["gemini_token"] == "fake-ephemeral-gemini-token"
    assert "control_token" in data
    assert "system-fake-api-key" not in token_res.text


def test_audio_test_token(client: TestClient) -> None:
    res = client.post("/live-audio-test/token")
    assert res.status_code == 200
    data = res.json()
    assert data["gemini_token"] == "fake-ephemeral-gemini-token"
    assert "websocket_url" in data


def test_control_websocket_lifecycle_and_sequence_ack(client: TestClient) -> None:
    create_res = client.post("/live-sessions", json=_sample_payload())
    session_id = create_res.json()["session_id"]

    token_res = client.post(f"/live-sessions/{session_id}/token")
    control_token = token_res.json()["control_token"]

    # 1. Unauthenticated or wrong token closes socket
    with client.websocket_connect(f"/ws/live-sessions/{session_id}/control") as ws:
        ws.send_json({"type": "authenticate", "token": "invalid-jwt-token"})
        error_msg = ws.receive_json()
        assert error_msg["type"] == "error"
        assert error_msg["code"] == "unauthorized"

    # 2. Authenticate and drive lifecycle
    with client.websocket_connect(f"/ws/live-sessions/{session_id}/control") as ws:
        ws.send_json({"type": "authenticate", "token": control_token})
        auth_ack = ws.receive_json()
        assert auth_ack["type"] == "authenticated"
        assert auth_ack["next_sequence"] == 1
        assert auth_ack["reconnect_after_seconds"] == 540

        # Start interview
        ws.send_json({"type": "started"})
        start_ack = ws.receive_json()
        assert start_ack["type"] == "started_ack"

        # Send sequence 1
        evt1 = _transcript_event(1, "interviewer", "Welcome to the interview.")
        ws.send_json({"type": "transcript_final", "event": evt1})
        ack1 = ws.receive_json()
        assert ack1 == {"type": "event_ack", "sequence": 1}

        # Send duplicate sequence 1 (replay): must echo sequence 1, not current server state!
        ws.send_json({"type": "transcript_final", "event": evt1})
        ack1_replay = ws.receive_json()
        assert ack1_replay == {"type": "event_ack", "sequence": 1}

        # Gap sequence rejected
        evt3 = _transcript_event(3, "candidate", "Skipped sequence.")
        ws.send_json({"type": "transcript_final", "event": evt3})
        conflict = ws.receive_json()
        assert conflict["type"] == "error"
        assert conflict["code"] == "conflict"

        # Sequence 2 accepted
        evt2 = _transcript_event(2, "candidate", "Thank you, excited to be here.")
        ws.send_json({"type": "transcript_final", "event": evt2})
        ack2 = ws.receive_json()
        assert ack2 == {"type": "event_ack", "sequence": 2}

        # Progress reporting
        ws.send_json({
            "type": "competency_progress",
            "call_id": "call-1",
            "progress": {"competency": "Python", "evidence_state": "supported"},
        })
        prog_ack = ws.receive_json()
        assert prog_ack == {"type": "tool_ack", "call_id": "call-1"}

        # Latency metric
        ws.send_json({
            "type": "latency_metric",
            "metric": {"name": "connection_setup", "duration_ms": 120.5},
        })
        assert ws.receive_json() == {"type": "metric_ack"}

        # Resumption handle
        ws.send_json({"type": "resumption_handle", "handle": "handle-12345"})
        assert ws.receive_json() == {"type": "resumption_handle_ack"}

        # Reconnecting
        ws.send_json({"type": "reconnecting", "reason": "network_switch"})
        assert ws.receive_json() == {"type": "reconnecting_ack"}


def test_control_websocket_completion_decision_enforces_wrap_up_gate(
    client: TestClient,
) -> None:
    create_res = client.post("/live-sessions", json=_sample_payload())
    session_id = create_res.json()["session_id"]
    token_res = client.post(f"/live-sessions/{session_id}/token")
    control_token = token_res.json()["control_token"]

    with client.websocket_connect(f"/ws/live-sessions/{session_id}/control") as ws:
        ws.send_json({"type": "authenticate", "token": control_token})
        ws.receive_json()

        ws.send_json({"type": "started"})
        ws.receive_json()

        # Send competency progress for Python
        ws.send_json({
            "type": "competency_progress",
            "call_id": "c1",
            "progress": {"competency": "Python", "evidence_state": "supported"},
        })
        assert ws.receive_json()["type"] == "tool_ack"

        # Send competency progress for System Design
        ws.send_json({
            "type": "competency_progress",
            "call_id": "c2",
            "progress": {"competency": "System Design", "evidence_state": "supported"},
        })
        assert ws.receive_json()["type"] == "tool_ack"

        # Request completion before wrap_up_at: all competencies supported, but wrap_up_at not reached
        ws.send_json({"type": "completion_request", "call_id": "comp-1", "reason": "all_done"})
        decision = ws.receive_json()
        assert decision["type"] == "completion_decision"
        assert decision["call_id"] == "comp-1"
        assert decision["approved"] is False
        assert decision["reason"] == "minimum_duration_not_reached"


def test_finish_endpoint_with_pending_events_fallback(client: TestClient) -> None:
    create_res = client.post("/live-sessions", json=_sample_payload())
    session_id = create_res.json()["session_id"]
    token_res = client.post(f"/live-sessions/{session_id}/token")
    control_token = token_res.json()["control_token"]

    # Start the session
    with client.websocket_connect(f"/ws/live-sessions/{session_id}/control") as ws:
        ws.send_json({"type": "authenticate", "token": control_token})
        ws.receive_json()
        ws.send_json({"type": "started"})
        ws.receive_json()

    # Client socket dropped, finishes via REST with unacknowledged events in body
    events = [
        _transcript_event(1, "interviewer", "Can you explain concurrency in Python?"),
        _transcript_event(2, "candidate", "We use asyncio for I/O and multiprocessing for CPU."),
    ]
    finish_res = client.post(
        f"/live-sessions/{session_id}/finish",
        json={"pending_events": events},
    )
    assert finish_res.status_code == 200
    data = finish_res.json()
    assert data["status"] == "sealed"
    assert data["transcript_persisted"] is True

    # Calling finish again is idempotent
    second_res = client.post(
        f"/live-sessions/{session_id}/finish",
        json={"pending_events": []},
    )
    assert second_res.status_code == 200
    assert second_res.json()["status"] == "sealed"


def test_ownership_enforcement_with_auth() -> None:
    settings = AppSettings(
        jwt_secret_key="test-jwt-secret-that-is-at-least-thirty-two-bytes-long",
        auth_enabled=True,
    )
    user_repo = InMemoryUserRepository()
    auth_service = AuthService(user_repository=user_repo, settings=settings)
    container = ServiceContainer(
        settings=settings,
        session_repository=InMemorySessionRepository(),
        transcript_repository=InMemoryTranscriptRepository(),
        job_repository=InMemoryJobRepository(),
        candidate_repository=InMemoryCandidateRepository(),
        application_repository=InMemoryApplicationRepository(),
        rubric_repository=InMemoryRubricRepository(),
        evaluation_repository=InMemoryEvaluationRepository(),
        user_repository=user_repo,
        bug_report_repository=InMemoryBugReportRepository(),
        audit_log_repository=InMemoryAuditLogRepository(),
        llm_credential_repository=InMemoryLLMCredentialRepository(),
        evaluation_dispatcher=AsyncTaskEvaluationDispatcher(),
        auth_provider=JWTAuthProvider(auth_service),
        persistence_is_ephemeral=True,
    )
    container.rubric_repository._rubrics["rub_job_live_001"] = _anchored_rubric("job_live_001")
    app = create_app(settings)
    app.state.container = container
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.gemini_live_token_factory = lambda: _make_test_token_service(
        settings.jwt_secret_key
    )

    client = TestClient(app)
    # Create candidate 1 and 2
    u1 = client.post(
        "/auth/signup",
        json={"email": "c1@example.com", "password": "password123", "user_type": "candidate"},
    ).json()
    token1 = u1["access_token"]
    user_id_1 = u1["user"]["user_id"]

    u2 = client.post(
        "/auth/signup",
        json={"email": "c2@example.com", "password": "password123", "user_type": "candidate"},
    ).json()
    token2 = u2["access_token"]
    user_id_2 = u2["user"]["user_id"]

    rec = client.post(
        "/auth/signup",
        json={"email": "rec@example.com", "password": "password123", "user_type": "recruiter"},
    ).json()
    rec_token = rec["access_token"]

    # Pre-register candidate 1 in candidate_repository
    cand_id_1 = "cand_001"
    container.candidate_repository._candidates[cand_id_1] = CandidateRecord(
        candidate_id=cand_id_1,
        user_id=user_id_1,
        full_name="Candidate One",
        email="c1@example.com",
        resume=ParsedResume(
            candidate_id=cand_id_1,
            candidate_name="Candidate One",
            email="c1@example.com",
            skills=["Python"],
        ),
    )

    # Candidate 1 creates live session
    payload = _sample_payload(candidate_id=cand_id_1)
    res = client.post(
        "/live-sessions",
        json=payload,
        headers={"Authorization": f"Bearer {token1}"},
    )
    assert res.status_code == 201
    session_id = res.json()["session_id"]

    # Candidate 1 can get the session
    owner_get = client.get(
        f"/live-sessions/{session_id}",
        headers={"Authorization": f"Bearer {token1}"},
    )
    assert owner_get.status_code == 200

    # Candidate 2 cannot get or finish candidate 1's session
    get_res = client.get(
        f"/live-sessions/{session_id}",
        headers={"Authorization": f"Bearer {token2}"},
    )
    assert get_res.status_code == 403

    finish_res = client.post(
        f"/live-sessions/{session_id}/finish",
        json={},
        headers={"Authorization": f"Bearer {token2}"},
    )
    assert finish_res.status_code == 403

    # Recruiter cannot finish candidate 1's live session
    rec_finish = client.post(
        f"/live-sessions/{session_id}/finish",
        json={},
        headers={"Authorization": f"Bearer {rec_token}"},
    )
    assert rec_finish.status_code == 403
