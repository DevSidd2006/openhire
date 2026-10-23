"""
Scoring lifecycle validation.

Re-initializing a session must not erase accumulated answers, and scoring an
unknown session must be reported as missing rather than represented by a
fabricated zero score.
"""
import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.errors import ConflictError, NotFoundError
from services.background_scoring import AnswerScore, BackgroundScoringService


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


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
    payload = {
        "session_id": "session_http_lifecycle",
        "candidate_id": "cand_http_lifecycle",
        "job_id": "job_http_lifecycle",
    }

    first = client.post("/scoring/initialize", json=payload)
    second = client.post("/scoring/initialize", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "initialized"
