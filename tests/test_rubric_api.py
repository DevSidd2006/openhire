"""Tests for the rubric lifecycle and screening leaderboard endpoints (Task 11).

The repo's .env sets AUTH_ENABLED=true with no real AuthProvider
installed, which makes the app refuse to start - a pre-existing environment
condition unrelated to these routes. These tests disable auth and reset the
cached settings so both create_app and the security dependencies agree.
"""
import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.routes.rubrics import get_match_leaderboard
from core.config import get_settings
from repositories.memory import (
    InMemoryApplicationRepository,
    InMemoryCandidateRepository,
    InMemoryJobRepository,
    InMemoryRubricRepository,
)
from repositories.interfaces import JobRecord
from schemas.application import Application, ApplicationStatus
from schemas.evaluation import MatchingScore
from schemas.job import JobDescription
from tests.rubric_fixtures import approved_rubric


@pytest.fixture
def client(monkeypatch):
    # get_settings() is lru_cached and read by the security dependencies as
    # well as by create_app, so the cache has to be cleared for the override
    # to be seen consistently by both.
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        get_settings.cache_clear()


_JD_TEXT = (
    "Senior Python Backend Developer. Build and operate async Python services "
    "on Kubernetes. Requires Python, REST APIs, SQL. 3+ years experience."
)


@pytest.fixture
def job_id(client):
    response = client.post("/jobs", json={"description": _JD_TEXT})
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def _draft(client, job_id):
    response = client.post(f"/jobs/{job_id}/rubric/draft")
    assert response.status_code == 201, response.text
    return response.json()


def test_drafting_a_new_version_does_not_replace_the_active_rubric(client, job_id):
    """A draft is not an approval. Job creation already auto-drafts and
    auto-approves version 1 (services/job_service.py - there is no
    recruiter step between posting a job and the leaderboard), so a fresh
    draft here is version 2, and version 1 must stay active until version 2
    is separately approved."""
    active_before = client.get(f"/jobs/{job_id}/rubric").json()
    assert active_before["status"] == "approved"
    assert active_before["version"] == 1

    draft = _draft(client, job_id)
    assert draft["status"] == "draft"
    assert draft["version"] == 2

    active = client.get(f"/jobs/{job_id}/rubric").json()
    assert active["rubric_id"] == active_before["rubric_id"], "drafting must never replace the active rubric"


def test_approving_makes_the_rubric_active(client, job_id):
    draft = _draft(client, job_id)
    approved = client.post(f"/jobs/{job_id}/rubric/{draft['rubric_id']}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    active = client.get(f"/jobs/{job_id}/rubric")
    assert active.status_code == 200
    assert active.json()["rubric_id"] == draft["rubric_id"]


def test_a_drafted_rubric_is_well_formed(client, job_id):
    draft = _draft(client, job_id)
    competencies = draft["competencies"]
    assert 3 <= len(competencies) <= 6
    assert sum(c["weight"] for c in competencies) == pytest.approx(1.0, abs=1e-3)
    for competency in competencies:
        assert sorted(competency["anchors"].keys()) == ["1", "2", "3", "4", "5"]
        assert all(text.strip() for text in competency["anchors"].values())


def test_approving_an_unknown_rubric_is_404(client, job_id):
    response = client.post(f"/jobs/{job_id}/rubric/rub_nonexistent/approve")
    assert response.status_code == 404


def test_approving_a_second_version_supersedes_the_first(client, job_id):
    """Exactly one approved rubric per job - otherwise leaderboard rows are
    scored under different rubrics and their ranks are incomparable."""
    first = _draft(client, job_id)
    client.post(f"/jobs/{job_id}/rubric/{first['rubric_id']}/approve")

    second = _draft(client, job_id)
    assert second["version"] == first["version"] + 1
    client.post(f"/jobs/{job_id}/rubric/{second['rubric_id']}/approve")

    active = client.get(f"/jobs/{job_id}/rubric").json()
    assert active["rubric_id"] == second["rubric_id"]

    versions = client.get(f"/jobs/{job_id}/rubric/versions").json()["versions"]
    assert [v for v in versions if v["status"] == "approved"] != []
    assert len([v for v in versions if v["status"] == "approved"]) == 1


def test_match_leaderboard_is_empty_before_anyone_applies(client, job_id):
    response = client.get(f"/jobs/{job_id}/match-leaderboard")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows"] == []
    assert body["needs_review"] == []


def test_match_leaderboard_on_unknown_job_is_404(client):
    response = client.get("/jobs/never-existed/match-leaderboard")
    assert response.status_code == 404


def test_match_leaderboard_reports_the_rubric_version_it_ranks_under(client, job_id):
    draft = _draft(client, job_id)
    client.post(f"/jobs/{job_id}/rubric/{draft['rubric_id']}/approve")

    body = client.get(f"/jobs/{job_id}/match-leaderboard").json()
    assert body["rubric_version"] == draft["version"]


def test_leaderboard_reports_the_auto_approved_rubric_version_immediately(client, job_id):
    """No manual draft/approve is needed for the ordinary flow: job creation
    already produced an approved rubric (version 1), so the leaderboard is
    scorable from the moment the job exists."""
    body = client.get(f"/jobs/{job_id}/match-leaderboard").json()
    assert body["rubric_version"] == 1


@pytest.mark.asyncio
async def test_flagged_but_scored_rows_are_ranked_and_remain_in_needs_review():
    job_id = "job_flagged"
    jobs = InMemoryJobRepository()
    applications = InMemoryApplicationRepository()
    candidates = InMemoryCandidateRepository()
    rubrics = InMemoryRubricRepository()

    await jobs.save(JobRecord(
        job_id=job_id,
        job=JobDescription(job_id=job_id, title="Engineer", description="Build things"),
    ))
    await rubrics.save(approved_rubric(job_id))

    flagged = MatchingScore(
        match_id="match_flagged", candidate_id="cand_flagged", job_id=job_id,
        match_score=0.8, coverage=0.25, band="strong",
        needs_human_review=True, explanation="partial evidence", confidence=0.7,
    )
    unscored = Application(
        application_id="app_pending", job_id=job_id, candidate_id="cand_pending",
        status=ApplicationStatus.SCORING_PENDING,
    )
    await applications.save(Application(
        application_id="app_flagged", job_id=job_id, candidate_id="cand_flagged",
        matching_score=flagged,
    ))
    await applications.save(unscored)

    response = await get_match_leaderboard(
        job_id=job_id,
        jobs=jobs,
        applications=applications,
        candidates=candidates,
        rubrics=rubrics,
        principal=None,
    )

    assert [row.candidate_id for row in response.rows] == ["cand_flagged"]
    assert response.rows[0].rank == 1
    assert [row.candidate_id for row in response.needs_review] == ["cand_flagged"]
    assert [row.candidate_id for row in response.scoring_pending] == ["cand_pending"]
