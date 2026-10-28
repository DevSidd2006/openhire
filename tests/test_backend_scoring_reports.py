"""
Scoring -> report integration.

The scoring API accumulates state in `BackgroundScoringService.scores` and
the report API reads that same state. Keeping the two services in separate
module-level globals made the documented flow impossible: a session
initialized through `/scoring/initialize` was never visible to
`/reports/generate`.

These tests pin the user-visible contract rather than the wiring detail:
initialize a session, generate a report for it, and read the stored report
back from the same app instance.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.routes import reports as report_routes
from api.routes import scoring as scoring_routes
from core.config import AppSettings
from core.dependencies import get_app_settings
from core.errors import ForbiddenError
from core.security import Principal, PrincipalType, require_authenticated
from repositories.interfaces import SessionRecord
from repositories.memory import InMemoryCandidateRepository, InMemorySessionRepository
from services.background_scoring import AnswerScore, BackgroundScoringService
from services.interview_report import InterviewReportGenerator
from utils.interview_session import SessionStatus


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_client():
    """Client whose auth boundary is enabled through dependency overrides.

    The real JWT provider is exercised elsewhere. These tests only need a
    stable Principal per request, so overriding `require_authenticated` keeps
    the ownership checks themselves under test without minting tokens.
    """
    app = create_app()
    current = {
        "principal": Principal(
            principal_type=PrincipalType.CANDIDATE,
            subject_id="user_owner",
        )
    }
    settings = AppSettings(auth_enabled=True)
    app.dependency_overrides[require_authenticated] = lambda: current["principal"]
    app.dependency_overrides[get_app_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client, current


def _seed_session(
    client: TestClient,
    session_id: str,
    *,
    candidate_id: str = "cand_report_1",
    job_id: str = "job_report_1",
) -> None:
    asyncio.run(
        client.app.state.container.session_repository.save(
            SessionRecord(
                session_id=session_id,
                candidate_id=candidate_id,
                job_id=job_id,
                status=SessionStatus.CREATED,
            )
        )
    )


def _initialize_session(client: TestClient, session_id: str = "session_report_1"):
    _seed_session(client, session_id)
    response = client.post(
        "/scoring/initialize",
        json={
            "session_id": session_id,
            "candidate_id": "cand_report_1",
            "job_id": "job_report_1",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "initialized"
    return session_id


def test_report_generation_reads_a_session_initialized_through_scoring(client):
    session_id = _initialize_session(client)

    response = client.post(
        "/reports/generate",
        json={
            "session_id": session_id,
            "candidate_name": "Jane Doe",
            "job_title": "Senior Backend Engineer",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "generated"
    assert body["report"]["session_id"] == session_id
    assert body["report"]["total_answers"] == 0


def test_generated_report_is_visible_through_the_report_endpoints(client):
    session_id = _initialize_session(client, session_id="session_report_2")

    generated = client.post(
        "/reports/generate",
        json={
            "session_id": session_id,
            "candidate_name": "Jane Doe",
            "job_title": "Senior Backend Engineer",
        },
    )
    assert generated.status_code == 200

    fetched = client.get(f"/reports/session/{session_id}")
    assert fetched.status_code == 200
    assert fetched.json()["session_id"] == session_id

    listed = client.get("/reports/list")
    assert listed.status_code == 200
    assert session_id in listed.json()["reports"]


def test_unknown_session_still_reports_not_found(client):
    response = client.post(
        "/reports/generate",
        json={
            "session_id": "session_never_initialized",
            "candidate_name": "Jane Doe",
            "job_title": "Senior Backend Engineer",
        },
    )

    assert response.status_code == 200
    assert response.json()["error"] == "Session not found or no scores available"


def _report_payload(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "candidate_name": "Jane Doe",
        "job_title": "Senior Backend Engineer",
    }


def _create_candidate(client: TestClient, name: str) -> str:
    response = client.post(
        "/candidates",
        json={"resume_text": f"{name} Python engineer", "candidate_name": name},
    )
    assert response.status_code == 201
    return response.json()["candidate_id"]


def _initialize_for(client: TestClient, session_id: str, candidate_id: str) -> None:
    _seed_session(
        client, session_id, candidate_id=candidate_id, job_id="job_report_1"
    )
    response = client.post(
        "/scoring/initialize",
        json={
            "session_id": session_id,
            "candidate_id": candidate_id,
            "job_id": "job_report_1",
        },
    )
    assert response.status_code == 200


def test_report_paths_enforce_candidate_ownership(auth_client):
    client, current = auth_client

    owner_candidate = _create_candidate(client, "Owner Candidate")
    _initialize_for(client, "session_owner", owner_candidate)
    assert client.post("/reports/generate", json=_report_payload("session_owner")).status_code == 200

    current["principal"] = Principal(
        principal_type=PrincipalType.CANDIDATE, subject_id="user_other"
    )
    other_candidate = _create_candidate(client, "Other Candidate")
    _initialize_for(client, "session_other", other_candidate)

    # The other candidate cannot generate a report for the owner's session.
    forbidden = client.post("/reports/generate", json=_report_payload("session_owner"))
    assert forbidden.status_code == 403

    assert client.post("/reports/generate", json=_report_payload("session_other")).status_code == 200

    # Ownership also governs retrieval and listing.
    assert client.get("/reports/session/session_owner").status_code == 403
    assert client.get("/reports/session/session_other").status_code == 200
    assert client.get("/reports/list").json()["reports"] == ["session_other"]

    # A recruiter can review either candidate's report, but cannot be used
    # as a way for a candidate to read somebody else's.
    current["principal"] = Principal(
        principal_type=PrincipalType.RECRUITER, subject_id="user_recruiter"
    )
    assert client.get("/reports/session/session_owner").status_code == 200
    assert client.get("/reports/session/session_other").status_code == 200
    assert set(client.get("/reports/list").json()["reports"]) == {
        "session_owner",
        "session_other",
    }


def test_retrieval_refreshes_a_report_generated_before_late_answers(client):
    session_id = _initialize_session(client, session_id="session_late_score")
    generated = client.post("/reports/generate", json=_report_payload(session_id))
    assert generated.status_code == 200
    assert generated.json()["report"]["total_answers"] == 0

    # Simulate the append that a score-answer request performs after its
    # mediator call completes while the report above was already stored.
    scoring = client.app.state.container.scoring_service.scores[session_id]
    scoring.add_answer(
        AnswerScore(
            question_id="q1",
            question_text="Describe a system you scaled.",
            answer_text="I partitioned the write path and cached reads.",
            competency="System Design",
            clarity=8,
            completeness=8,
            relevance=8,
        )
    )

    fetched = client.get(f"/reports/session/{session_id}")
    assert fetched.status_code == 200
    assert fetched.json()["total_answers"] == 1


@pytest.mark.asyncio
async def test_legacy_scoring_session_can_generate_and_read_report_with_auth_enabled():
    sessions = InMemorySessionRepository()
    candidates = InMemoryCandidateRepository()
    scoring = BackgroundScoringService()
    reports = InterviewReportGenerator()
    principal = Principal(principal_type=PrincipalType.CANDIDATE, subject_id="legacy_user")
    settings = AppSettings(auth_enabled=True)
    await sessions.save(SessionRecord(
        session_id="legacy_report", candidate_id="legacy_candidate",
        job_id="job_report", status=SessionStatus.CREATED,
    ))

    initialized = await scoring_routes.initialize_scoring(
        scoring_routes.InitializeScoringRequest(
            session_id="legacy_report", candidate_id="legacy_candidate", job_id="job_report",
        ),
        service=scoring, sessions=sessions, candidates=candidates,
        principal=principal, settings=settings,
    )
    assert initialized["status"] == "initialized"

    generated = await report_routes.generate_report(
        report_routes.GenerateReportRequest(
            session_id="legacy_report", candidate_name="Legacy", job_title="Engineer",
        ),
        generator=reports, scoring_service=scoring, session_repository=sessions,
        candidate_repository=candidates, principal=principal, settings=settings,
    )
    assert generated["status"] == "generated"
    fetched = await report_routes.get_report(
        "legacy_report", generator=reports, scoring_service=scoring,
        session_repository=sessions, candidate_repository=candidates,
        principal=principal, settings=settings,
    )
    assert fetched["session_id"] == "legacy_report"


@pytest.mark.asyncio
async def test_owner_keeps_report_access_after_scores_are_cleared():
    sessions = InMemorySessionRepository()
    scoring = BackgroundScoringService()
    reports = InterviewReportGenerator()
    principal = Principal(principal_type=PrincipalType.CANDIDATE, subject_id="owner")
    settings = AppSettings(auth_enabled=True)

    class Candidates:
        async def get(self, candidate_id):
            return SimpleNamespace(user_id="owner") if candidate_id == "owned_candidate" else None

    candidates = Candidates()
    await sessions.save(SessionRecord(
        session_id="owned_report", candidate_id="owned_candidate",
        job_id="job_report", status=SessionStatus.CREATED,
    ))
    await scoring.initialize_session("owned_report", "owned_candidate", "job_report")
    await report_routes.generate_report(
        report_routes.GenerateReportRequest(
            session_id="owned_report", candidate_name="Owner", job_title="Engineer",
        ),
        generator=reports, scoring_service=scoring, session_repository=sessions,
        candidate_repository=candidates, principal=principal, settings=settings,
    )

    scoring.clear_session("owned_report")
    fetched = await report_routes.get_report(
        "owned_report", generator=reports, scoring_service=scoring,
        session_repository=sessions, candidate_repository=candidates,
        principal=principal, settings=settings,
    )
    assert fetched["session_id"] == "owned_report"
    listed = await report_routes.list_reports(
        generator=reports, session_repository=sessions,
        candidate_repository=candidates, principal=principal, settings=settings,
    )
    assert listed["reports"] == ["owned_report"]

    outsider = Principal(principal_type=PrincipalType.CANDIDATE, subject_id="outsider")
    with pytest.raises(ForbiddenError):
        await report_routes.get_report(
            "owned_report", generator=reports, scoring_service=scoring,
            session_repository=sessions, candidate_repository=candidates,
            principal=outsider, settings=settings,
        )
