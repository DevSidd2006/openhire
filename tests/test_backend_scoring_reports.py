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
import pytest
from fastapi.testclient import TestClient

from api.app import create_app


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def _initialize_session(client: TestClient, session_id: str = "session_report_1"):
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
