"""
Scoring lifecycle validation.

Re-initializing a session must not erase accumulated answers, and scoring an
unknown session must be reported as missing rather than represented by a
fabricated zero score.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.routes.scoring import get_scoring_service
from core.config import AppSettings, get_settings
from core.errors import ConflictError, DependencyError, NotFoundError
from core.security import Principal, PrincipalType, require_authenticated
from repositories.interfaces import SessionRecord
from services.background_scoring import (
    AnswerScore,
    BackgroundScoringService,
    InterviewScoring,
)
from utils.interview_session import SessionStatus


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_client():
    app = create_app()
    current = {
        "principal": Principal(
            principal_type=PrincipalType.CANDIDATE,
            subject_id="user_owner",
        )
    }
    settings = AppSettings(auth_enabled=True)
    app.dependency_overrides[require_authenticated] = lambda: current["principal"]
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client, current


def _seed_session(
    client: TestClient,
    session_id: str,
    *,
    candidate_id: str = "cand_http_lifecycle",
    job_id: str = "job_http_lifecycle",
) -> None:
    record = SessionRecord(
        session_id=session_id,
        candidate_id=candidate_id,
        job_id=job_id,
        status=SessionStatus.CREATED,
    )
    asyncio.run(client.app.state.container.session_repository.save(record))


def _initialize_payload(
    session_id: str,
    *,
    candidate_id: str = "cand_http_lifecycle",
    job_id: str = "job_http_lifecycle",
) -> dict:
    return {
        "session_id": session_id,
        "candidate_id": candidate_id,
        "job_id": job_id,
    }


@pytest.mark.asyncio
async def test_reinitializing_a_session_preserves_accumulated_answers():
    service = BackgroundScoringService()
    scoring = await service.initialize_session(
        session_id="session_lifecycle",
        candidate_id="cand_lifecycle",
        job_id="job_lifecycle",
    )
    scoring.add_answer(
        AnswerScore(
            question_id="q1",
            question_text="Question",
            answer_text="Answer",
            competency="Python",
            clarity=8,
            completeness=8,
            relevance=8,
        )
    )

    again = await service.initialize_session(
        session_id="session_lifecycle",
        candidate_id="cand_lifecycle",
        job_id="job_lifecycle",
    )

    assert again is scoring
    assert len(again.answers) == 1


@pytest.mark.asyncio
async def test_reinitializing_with_a_different_candidate_is_a_conflict():
    service = BackgroundScoringService()
    await service.initialize_session(
        session_id="session_lifecycle",
        candidate_id="cand_first",
        job_id="job_lifecycle",
    )

    with pytest.raises(ConflictError):
        await service.initialize_session(
            session_id="session_lifecycle",
            candidate_id="cand_second",
            job_id="job_lifecycle",
        )


@pytest.mark.asyncio
async def test_scoring_an_unknown_session_raises_not_found():
    service = BackgroundScoringService()

    with pytest.raises(NotFoundError):
        await service.score_answer(
            session_id="session_never_initialized",
            question_id="q1",
            question_text="Question",
            answer_text="Answer",
            competency="Python",
            candidate_name="Jane Doe",
        )


def test_score_answer_for_an_unknown_session_returns_404(client):
    response = client.post(
        "/scoring/score-answer",
        json={
            "session_id": "session_never_initialized",
            "question_id": "q1",
            "question_text": "Question",
            "answer_text": "Answer",
            "competency": "Python",
            "candidate_name": "Jane Doe",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_reinitializing_through_http_is_idempotent(client):
    _seed_session(client, "session_http_lifecycle")
    payload = _initialize_payload("session_http_lifecycle")

    first = client.post("/scoring/initialize", json=payload)
    second = client.post("/scoring/initialize", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "initialized"


def test_conflicting_initialization_through_http_is_409(client):
    _seed_session(client, "session_http_conflict")
    first = client.post(
        "/scoring/initialize", json=_initialize_payload("session_http_conflict")
    )
    assert first.status_code == 200

    conflict = client.post(
        "/scoring/initialize",
        json=_initialize_payload(
            "session_http_conflict",
            candidate_id="cand_other",
            job_id="job_other",
        ),
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"] == "conflict"


def test_initialization_requires_an_existing_interview_session(client):
    response = client.post(
        "/scoring/initialize", json=_initialize_payload("session_no_interview")
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_provider_failure_raises_instead_of_returning_zero_scores():
    class _BrokenMediator:
        async def analyze_response(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    service = BackgroundScoringService()
    await service.initialize_session(
        session_id="session_provider_failure",
        candidate_id="cand_provider_failure",
        job_id="job_provider_failure",
    )
    service.mediator_agent = _BrokenMediator()

    with pytest.raises(DependencyError):
        await service.score_answer(
            session_id="session_provider_failure",
            question_id="q1",
            question_text="Question",
            answer_text="Answer",
            competency="Python",
            candidate_name="Jane Doe",
        )

    scoring = await service.get_session_scores("session_provider_failure")
    assert scoring is not None
    assert scoring.answers == []


def test_provider_failure_through_http_is_503(client):
    class _BrokenMediator:
        async def analyze_response(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    service = BackgroundScoringService()
    service.scores["session_provider_failure_http"] = InterviewScoring(
        session_id="session_provider_failure_http",
        candidate_id="candidate_provider_failure_http",
        job_id="job_provider_failure_http",
    )
    service.mediator_agent = _BrokenMediator()
    client.app.dependency_overrides[get_scoring_service] = lambda: service

    response = client.post(
        "/scoring/score-answer",
        json={
            "session_id": "session_provider_failure_http",
            "question_id": "q1",
            "question_text": "Question",
            "answer_text": "Answer",
            "competency": "Python",
            "candidate_name": "Jane Doe",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"] == "dependency_unavailable"


def test_initialization_requires_candidate_ownership_when_auth_is_enabled(auth_client):
    client, current = auth_client
    owner_candidate = client.post(
        "/candidates",
        json={"resume_text": "Owner Python engineer", "candidate_name": "Owner"},
    ).json()["candidate_id"]
    _seed_session(
        client, "session_owned", candidate_id=owner_candidate, job_id="job_owned"
    )
    payload = _initialize_payload(
        "session_owned", candidate_id=owner_candidate, job_id="job_owned"
    )

    assert client.post("/scoring/initialize", json=payload).status_code == 200

    current["principal"] = Principal(
        principal_type=PrincipalType.CANDIDATE, subject_id="user_other"
    )
    forbidden = client.post("/scoring/initialize", json=payload)
    assert forbidden.status_code == 403

    current["principal"] = Principal(
        principal_type=PrincipalType.RECRUITER, subject_id="user_recruiter"
    )
    assert client.post("/scoring/initialize", json=payload).status_code == 200

    current["principal"] = Principal(
        principal_type=PrincipalType.CANDIDATE,
        subject_id="user_other",
        session_id="session_owned",
    )
    assert client.post("/scoring/initialize", json=payload).status_code == 200
