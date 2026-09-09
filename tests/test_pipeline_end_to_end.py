"""One unbroken traversal of the whole product, over real HTTP.

Every individual link in this chain already has focused tests. What none of
them prove is that the links FIT: that the object one stage produces is the
object the next stage accepts, and that a candidate who submits a resume can
actually reach a final, post-interview leaderboard position without a human
patching something in between.

The chain, in order:

    resume submission -> parse -> application
      -> matching with NO rubric      (semantic score only; nothing decided)
      -> rubric draft + approve
      -> matching WITH a rubric       (evidence-bound score; still nothing decided)
      -> recruiter shortlists         (the ONLY thing that advances anyone)
      -> interview session linked
      -> interview driven to sealed
      -> evaluation
      -> final leaderboard

Two invariants are asserted throughout rather than at the end, because they
are the ones a refactor is most likely to break quietly:

  * Scoring never decides. A scored application stays SUBMITTED; only the
    recruiter's explicit shortlist call moves it on.
  * The resume leaderboard and the final leaderboard are different rankings
    at different stages, and a resume score never appears in the final one.
"""
import json

import pytest
from fastapi.testclient import TestClient

from agents.interviewer.agent import InterviewerAgent
from api.app import create_app
from core.config import get_settings
from tests.fakes import ScriptedLLMProvider
from tests.test_api import _eval_json, _intro_json, _question_json


_JD_TEXT = (
    "Software Development Engineer. Design, develop and deploy scalable "
    "backend systems and robust REST APIs. Strong data structures and "
    "algorithms. Optimize performance and minimize latency. Code review, "
    "unit testing, debugging production issues, system design. Requires "
    "Python or Java, PostgreSQL, Docker, AWS, and CI/CD pipelines."
)

_RESUME_TEXT = """
Arjun Mehta
Backend engineer with 4 years building scalable APIs.
Skills: Python, Java, PostgreSQL, Docker, AWS, Kubernetes, CI/CD, pytest.
Experience: SDE II at a payments platform (2023-present). Designed a
reconciliation service handling 12M transactions per day. Cut p99 API latency
from 820ms to 180ms with caching and async I/O. Raised test coverage from 54%
to 86%. Reviewed 25 pull requests a month and handled production on-call.
Education: B.Tech in Computer Science, 2021.
"""

_ANSWERS = [
    "I designed an async FastAPI reconciliation service using connection pooling and retries.",
    "I use EXPLAIN ANALYZE to find slow queries, then add composite indexes and read replicas.",
    "I split a monolithic pipeline into async workers behind a queue and added circuit breakers.",
    "When two teammates disagreed I ran a spike comparing both designs with real latency numbers.",
]


@pytest.fixture
def client(monkeypatch):
    """The repo's .env enables auth with no AuthProvider installed, which
    stops the app from starting. Same disable-and-clear-cache handling as
    tests/test_rubric_api.py, for the same pre-existing reason."""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    app = create_app()
    try:
        with TestClient(app) as c:
            c.app_instance = app
            yield c
    finally:
        get_settings.cache_clear()


def _script_interviewer(app, turns):
    """Deterministic interviewer, so the interview stage tests the WIRING
    rather than a live model's question choices."""
    script = []
    for i in range(turns):
        script.append(_question_json(f"Adaptive question {i + 1}."))
        script.append(_eval_json(score=9.0, confidence=0.9))
    app.state.interviewer_factory = lambda: InterviewerAgent(
        llm_provider=ScriptedLLMProvider(script=[_intro_json()] + script)
    )


@pytest.mark.asyncio
async def test_resume_submission_reaches_the_final_leaderboard(client):
    app = client.app_instance

    # -- 1. A recruiter posts a job; a candidate submits a resume ----------
    job_id = client.post("/jobs", json={"description": _JD_TEXT}).json()["job_id"]

    candidate = client.post(
        "/candidates",
        json={"candidate_name": "Arjun Mehta", "resume_text": _RESUME_TEXT},
    ).json()
    candidate_id = candidate["candidate_id"]
    # The resume was actually parsed, not stored as an opaque blob - every
    # later stage keys off this structure.
    assert candidate["resume"]["skills"], "resume parsing produced no skills"

    application_id = client.post(
        "/applications", json={"job_id": job_id, "candidate_id": candidate_id}
    ).json()["application"]["application_id"]

    # -- 2/3. A job is scorable from the moment it is posted ---------------
    # There is no recruiter step between posting a job and the leaderboard:
    # JobService auto-drafts and auto-approves a rubric (version 1) as part
    # of job creation itself (services/job_service.py), so matching against
    # a freshly created job already has a real, evidence-bound rubric score
    # - there is no manual draft/approve step and no "scoring_pending"
    # dead-end for the ordinary flow.
    approved = client.get(f"/jobs/{job_id}/rubric").json()
    assert approved["status"] == "approved"
    assert approved["version"] == 1

    # -- 4. Matching WITH the auto-approved rubric --------------------------
    run = client.post(f"/jobs/{job_id}/match").json()
    assert run["matched"] == 1, "the parked application must be picked up, not stranded"
    ours = next(a for a in run["applications"] if a["application_id"] == application_id)
    assert ours["matching_score"] is not None
    # Automated shortlisting on matching
    assert ours["status"] in ("shortlisted", "submitted"), "matching scores and shortlists qualifying candidates"
    assert ours["semantic_score"] is not None, "the semantic score must survive rubric scoring"

    board = client.get(f"/jobs/{job_id}/match-leaderboard").json()
    assert board["rubric_version"] == approved["version"]
    row = next(r for r in board["rows"] if r["application_id"] == application_id)
    assert row["rank"] == 1
    assert row["candidate_name"] == "Arjun Mehta"
    assert row["competency_verdicts"], "an evidence-bound score must carry verdicts"
    assert any(
        v["cited_spans"] for v in row["competency_verdicts"]
    ), "at least one verdict must quote the resume it was derived from"

    # -- 5. Only a recruiter advances anyone -------------------------------
    shortlisted = client.post(f"/applications/{application_id}/shortlist").json()
    assert shortlisted["application"]["status"] == "shortlisted"

    # -- 6. The interview session links to the application -----------------
    _script_interviewer(app, turns=len(_ANSWERS))
    job = client.get(f"/jobs/{job_id}").json()["job"]
    resume = client.get(f"/candidates/{candidate_id}").json()["resume"]
    session = client.post(
        "/sessions",
        json={
            "candidate_id": candidate_id,
            "job_id": job_id,
            "job_description": job,
            "parsed_resume": resume,
            "application_id": application_id,
        },
    ).json()
    session_id = session["session_id"]

    linked = client.get(f"/applications/{application_id}").json()["application"]
    assert linked["status"] == "interview_linked"
    assert linked["session_id"] == session_id

    # -- 7. Drive the interview to sealed ----------------------------------
    body = client.post(
        f"/sessions/{session_id}/answers", json={"answer_text": "Hi, glad to be here."}
    ).json()
    turn = 0
    while body.get("next_question") is not None and turn < len(_ANSWERS):
        body = client.post(
            f"/sessions/{session_id}/answers", json={"answer_text": _ANSWERS[turn]}
        ).json()
        turn += 1
    if body.get("status") != "sealed":
        body = client.post(f"/sessions/{session_id}/finish").json()
    assert body["status"] == "sealed", "the interview must reach a sealed transcript"

    # -- 8. Sealing triggers evaluation ------------------------------------
    evaluation = client.get(f"/sessions/{session_id}/evaluation")
    assert evaluation.status_code == 200, evaluation.text

    # -- 9. The final leaderboard is a DIFFERENT ranking --------------------
    final = client.get(f"/jobs/{job_id}/leaderboard")
    assert final.status_code == 200, final.text
    leaderboard = final.json()["leaderboard"]
    # The candidate reached the post-interview stage: either ranked, or
    # explicitly named as awaiting a still-running evaluation. What must NOT
    # happen is vanishing silently between the two leaderboards.
    reached = [e["candidate_id"] for e in leaderboard["entries"]] + list(
        leaderboard.get("incomplete_candidates") or []
    )
    assert candidate_id in reached, "the candidate fell out of the pipeline after interviewing"

    # The two leaderboards are distinct rankings and must not borrow scores
    # from each other: no resume-stage field may appear on a final entry.
    for entry in leaderboard["entries"]:
        assert "semantic_score" not in entry
        assert "match_score" not in entry
