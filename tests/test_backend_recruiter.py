"""
Chunk 5: recruiter-facing APIs, reports and leaderboard.

Covers the NEW recruiter aggregation (`RecruiterService`) and the NEW
routes it backs (`GET /jobs/{id}/applications`, `GET /jobs/{id}/leaderboard`,
`GET /applications/{id}/interview`, `GET /applications/{id}/evaluation`,
`POST /applications/{id}/shortlist`, `POST /applications/{id}/reject`) - no
agent, scoring formula, or evaluation logic is reimplemented here.
`LeaderboardAgent` (agents/leaderboard/agent.py) is exercised directly and
unmodified; every leaderboard entry's score is asserted to come from the
exact `CandidateReport` an evaluation already produced (Chunk 4), never a
second computation.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from repositories.interfaces import Application, EvaluationStatus
from repositories.memory import (
    InMemoryApplicationRepository,
    InMemoryCandidateRepository,
    InMemoryEvaluationRepository,
    InMemoryJobRepository,
    InMemorySessionRepository,
    InMemoryTranscriptRepository,
)
from schemas.application import ApplicationStatus
from schemas.evaluation import MatchingScore
from schemas.job import Competency, JobDescription
from schemas.resume import ParsedResume
from services.evaluation_service import EvaluationAgentFactories, EvaluationService
from services.interview_service import InterviewService
from services.recruiter_service import RecruiterService

# ---------------------------------------------------------------------------
# Test-only deterministic dispatcher (same pattern as test_backend_evaluation.py)
# ---------------------------------------------------------------------------

class RecordingDispatcher:
    def __init__(self):
        self.scheduled = []

    def schedule(self, evaluation_id, run):
        self.scheduled.append((evaluation_id, run))

    async def run_all(self):
        pending, self.scheduled = self.scheduled, []
        for _evaluation_id, run in pending:
            await run()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job(job_id="job_rec"):
    return JobDescription(
        job_id=job_id, title="Backend Engineer", description="Test role",
        required_skills=["Python"],
        competencies=[Competency(name="Python", weight=1.0)],
    )


def _resume(candidate_id, name):
    return ParsedResume(candidate_id=candidate_id, candidate_name=name, skills=["Python"])


def _matching_score(candidate_id, job_id, score=0.9):
    return MatchingScore(
        match_id=f"match_{candidate_id}", candidate_id=candidate_id, job_id=job_id,
        match_score=score, skill_matches=["Python"], experience_match=0.9,
        skill_gap=0.1, explanation="match", shortlist_recommendation=True, confidence=0.9,
    )


def _question_json(text):
    return json.dumps({
        "question_text": text, "question_type": "initial", "difficulty": "medium",
        "reason": "x", "expected_duration_seconds": 60,
    })


def _eval_json(score=8.0, confidence=0.9):
    return json.dumps({
        "score": score, "confidence": confidence, "evidence_status": "supported",
        "is_vague": False, "missing_detail": None, "explanation": "x",
    })


def _scripted_interviewer():
    from agents.interviewer.agent import InterviewerAgent
    from tests.fakes import ScriptedLLMProvider

    # Generate enough question/eval pairs for an interview to complete.
    # The interview asks: intro question, then evaluates, then asks follow-ups
    # until sufficient coverage is reached.
    script = []
    for i in range(10):  # 10 question/eval pairs should be enough
        script.append(_question_json(f"Q{i+1}?"))
        script.append(_eval_json(confidence=0.85))  # High enough to count as coverage

    return lambda: InterviewerAgent(
        llm_provider=ScriptedLLMProvider(script=script)
    )


class RecruiterHarness:
    """Everything one recruiter-layer test needs, sharing one set of
    in-memory repositories - mirrors test_backend_evaluation.py's _Harness."""

    def __init__(self):
        from api.registry import SessionRegistry

        self.jobs = InMemoryJobRepository()
        self.candidates = InMemoryCandidateRepository()
        self.applications = InMemoryApplicationRepository()
        self.sessions = InMemorySessionRepository()
        self.transcripts = InMemoryTranscriptRepository()
        self.evaluations = InMemoryEvaluationRepository()
        self.dispatcher = RecordingDispatcher()

        self.interview_service = InterviewService(
            registry=SessionRegistry(), session_repository=self.sessions,
            transcript_repository=self.transcripts, interviewer_factory=_scripted_interviewer(),
        )
        self.evaluation_service = EvaluationService(
            evaluation_repository=self.evaluations, session_repository=self.sessions,
            transcript_repository=self.transcripts, application_repository=self.applications,
            dispatcher=self.dispatcher,
        )
        self.recruiter_service = RecruiterService(
            job_repository=self.jobs, application_repository=self.applications,
            candidate_repository=self.candidates, session_repository=self.sessions,
            evaluation_repository=self.evaluations,
        )

    async def seed_job(self, job_id="job_rec"):
        from repositories.interfaces import JobRecord

        await self.jobs.save(JobRecord(job_id=job_id, job=_job(job_id)))

    async def seed_candidate(self, candidate_id, name="Candidate"):
        from repositories.interfaces import CandidateRecord

        await self.candidates.save(CandidateRecord(
            candidate_id=candidate_id, user_id="user_rec", resume=_resume(candidate_id, name)
        ))

    async def seed_application(
        self, *, application_id, candidate_id, job_id="job_rec", status=ApplicationStatus.SHORTLISTED,
        matching_score_value=0.9,
    ) -> Application:
        return await self.applications.save(Application(
            application_id=application_id, job_id=job_id, candidate_id=candidate_id,
            status=status,
            matching_score=_matching_score(candidate_id, job_id, matching_score_value) if status != ApplicationStatus.SUBMITTED else None,
        ))

    async def seal_interview(self, *, application_id, candidate_id, job_id="job_rec") -> str:
        session_id, runner = await self.interview_service.create_session(
            job_description=_job(job_id), parsed_resume=_resume(candidate_id, "x"),
            candidate_id=candidate_id, max_questions=1, application_id=application_id,
        )
        while not runner.is_finished():
            result = await self.interview_service.submit_answer(session_id, "A detailed answer.")
            if result.next_question is None:
                break
        application = await self.applications.get(application_id)
        await self.applications.save(application.model_copy(
            update={"session_id": session_id, "status": ApplicationStatus.INTERVIEW_LINKED}
        ))
        return session_id

    async def run_evaluation(self, session_id) -> str:
        job = await self.evaluation_service.trigger_evaluation(session_id)
        await self.dispatcher.run_all()
        return job.evaluation_id


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 2/3/18. Job applications view: composite, batch-efficient, defensive
# ---------------------------------------------------------------------------

class TestJobApplicationsView:
    @pytest.mark.asyncio
    async def test_overview_assembles_candidate_interview_and_evaluation(self):
        h = RecruiterHarness()
        await h.seed_job()
        await h.seed_candidate("cand_1", "Jane Doe")
        await h.seed_application(application_id="app_1", candidate_id="cand_1")
        session_id = await h.seal_interview(application_id="app_1", candidate_id="cand_1")
        await h.run_evaluation(session_id)

        overviews = await h.recruiter_service.list_job_applications("job_rec")
        assert len(overviews) == 1
        o = overviews[0]
        assert o.application.application_id == "app_1"
        assert o.candidate.resume.candidate_name == "Jane Doe"
        assert o.session.status.value == "sealed"
        assert o.evaluation.status == EvaluationStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_overview_is_defensive_when_candidate_record_is_missing(self):
        """Step 18: a candidate record can legitimately be absent (e.g. a
        future data-retention deletion) - the overview must degrade to
        `candidate=None`, never raise."""
        h = RecruiterHarness()
        await h.seed_job()
        # No seed_candidate() call - application references a candidate_id
        # with no CandidateRecord at all.
        await h.seed_application(application_id="app_orphan", candidate_id="cand_ghost")

        overviews = await h.recruiter_service.list_job_applications("job_rec")
        assert overviews[0].candidate is None

    @pytest.mark.asyncio
    async def test_overview_before_interview_has_no_session_or_evaluation(self):
        h = RecruiterHarness()
        await h.seed_job()
        await h.seed_candidate("cand_2")
        await h.seed_application(application_id="app_2", candidate_id="cand_2")

        overviews = await h.recruiter_service.list_job_applications("job_rec")
        assert overviews[0].session is None
        assert overviews[0].evaluation is None

    @pytest.mark.asyncio
    async def test_unknown_job_is_not_found(self):
        h = RecruiterHarness()
        from core.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await h.recruiter_service.list_job_applications("never-existed")

    def test_http_endpoint_returns_the_overview(self, client, app):
        _seed_http(app, "cand_http_1", "job_http_1", "app_http_1", run_eval=True)
        response = client.get("/jobs/job_http_1/applications")
        assert response.status_code == 200
        body = response.json()
        assert body["job_id"] == "job_http_1"
        assert body["total"] == 1
        row = body["applications"][0]
        assert row["candidate"]["candidate_id"] == "cand_http_1"
        assert row["interview_status"]["status"] == "sealed"
        assert row["evaluation_status"] == "completed"

    def test_http_endpoint_404s_for_unknown_job(self, client):
        response = client.get("/jobs/never-existed/applications")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# 5/6/17. Interview status and evaluation status per application
# ---------------------------------------------------------------------------

class TestApplicationInterviewAndEvaluationStatus:
    def test_interview_status_before_any_interview_is_null(self, client, app):
        _seed_http(app, "cand_status_1", "job_status_1", "app_status_1", run_eval=False, seal=False)
        response = client.get("/applications/app_status_1/interview")
        assert response.status_code == 200
        assert response.json()["interview"] is None

    def test_interview_status_reflects_the_real_session_record(self, client, app):
        _seed_http(app, "cand_status_2", "job_status_2", "app_status_2", run_eval=False, seal=True)
        response = client.get("/applications/app_status_2/interview")
        body = response.json()["interview"]
        assert body["status"] == "sealed"
        assert body["questions_answered"] == 1

    def test_evaluation_status_before_triggered_is_null(self, client, app):
        _seed_http(app, "cand_status_3", "job_status_3", "app_status_3", run_eval=False, seal=False)
        response = client.get("/applications/app_status_3/evaluation")
        assert response.status_code == 200
        assert response.json()["evaluation"] is None

    def test_evaluation_status_matches_sessions_evaluation_endpoint(self, client, app):
        """Step 17-adjacent: the application-centric view and the
        session-centric view (Chunk 4) must never disagree - same
        EvaluationJob, two lookup paths."""
        session_id, _eval_id = _seed_http(
            app, "cand_status_4", "job_status_4", "app_status_4", run_eval=True,
        )
        via_application = client.get("/applications/app_status_4/evaluation").json()
        via_session = client.get(f"/sessions/{session_id}/evaluation").json()
        assert via_application["evaluation"]["evaluation_id"] == via_session["evaluation"]["evaluation_id"]
        assert via_application["evaluation"]["status"] == via_session["evaluation"]["status"]

    def test_unknown_application_is_404_for_both_views(self, client):
        assert client.get("/applications/never-existed/interview").status_code == 404
        assert client.get("/applications/never-existed/evaluation").status_code == 404


# ---------------------------------------------------------------------------
# 4. Manual shortlist / reject overrides
# ---------------------------------------------------------------------------

class TestManualShortlistReject:
    def test_shortlist_reverses_a_rejection(self, client, app):
        job_id, candidate_id, application_id = _seed_bare_application(app, status="rejected")
        response = client.post(f"/applications/{application_id}/shortlist")
        assert response.status_code == 200
        assert response.json()["application"]["status"] == "shortlisted"

    def test_shortlist_is_refused_once_interview_linked(self, client, app):
        _seed_http(app, "cand_sl_1", "job_sl_1", "app_sl_1", run_eval=False, seal=True)
        response = client.post("/applications/app_sl_1/shortlist")
        assert response.status_code == 409

    def test_reject_works_even_after_a_completed_evaluation(self, client, app):
        """The most common real use: reviewing a completed report and
        deciding not to proceed."""
        _seed_http(app, "cand_rj_1", "job_rj_1", "app_rj_1", run_eval=True)
        response = client.post("/applications/app_rj_1/reject")
        assert response.status_code == 200
        assert response.json()["application"]["status"] == "rejected"

    def test_reject_is_idempotent(self, client, app):
        job_id, candidate_id, application_id = _seed_bare_application(app, status="rejected")
        response = client.post(f"/applications/{application_id}/reject")
        assert response.status_code == 200
        assert response.json()["application"]["status"] == "rejected"

    def test_unknown_application_is_404(self, client):
        assert client.post("/applications/never-existed/shortlist").status_code == 404
        assert client.post("/applications/never-existed/reject").status_code == 404


# ---------------------------------------------------------------------------
# 11/12/13/14/19. Leaderboard: ranking, score consistency, ties, filters, pagination
# ---------------------------------------------------------------------------

class TestLeaderboard:
    @pytest.mark.asyncio
    async def test_ranks_by_the_same_score_the_report_carries(self):
        """Step 12/17: the leaderboard must never show a different number
        than the report it came from."""
        h = RecruiterHarness()
        await h.seed_job()
        await h.seed_candidate("cand_a", "Alice")
        await h.seed_application(application_id="app_a", candidate_id="cand_a")
        session_a = await h.seal_interview(application_id="app_a", candidate_id="cand_a")
        eval_a = await h.run_evaluation(session_a)

        result = await h.recruiter_service.get_leaderboard("job_rec")
        entry = result.leaderboard.entries[0]
        stored = await h.evaluations.get(eval_a)
        assert entry.weighted_score == stored.result.scores.weighted_final_score
        assert entry.candidate_id == "cand_a"

    @pytest.mark.asyncio
    async def test_incomplete_candidates_are_listed_not_silently_dropped(self):
        h = RecruiterHarness()
        await h.seed_job()
        await h.seed_candidate("cand_b", "Bob")
        # Shortlisted, but never interviewed - no session, no evaluation.
        await h.seed_application(application_id="app_b", candidate_id="cand_b")

        result = await h.recruiter_service.get_leaderboard("job_rec")
        assert result.leaderboard.entries == []
        assert "cand_b" in result.leaderboard.incomplete_candidates

    @pytest.mark.asyncio
    async def test_rejected_and_submitted_applications_are_excluded_from_ranking(self):
        h = RecruiterHarness()
        await h.seed_job()
        await h.seed_candidate("cand_c", "Carl")
        await h.seed_application(application_id="app_c", candidate_id="cand_c",
                                 status=ApplicationStatus.REJECTED)
        await h.seed_candidate("cand_d", "Dana")
        await h.seed_application(application_id="app_d", candidate_id="cand_d",
                                 status=ApplicationStatus.SUBMITTED)

        result = await h.recruiter_service.get_leaderboard("job_rec")
        assert result.leaderboard.entries == []
        assert result.leaderboard.incomplete_candidates == []  # never in the pool at all

    @pytest.mark.asyncio
    async def test_deterministic_tie_break_by_candidate_id(self):
        """Step 9: two identical scores must rank in a defined,
        content-derived order (candidate_id ascending), never incidental
        repository/list order."""
        h = RecruiterHarness()
        await h.seed_job()
        for cid, name in [("cand_z", "Zara"), ("cand_a", "Aaron")]:
            await h.seed_candidate(cid, name)
            await h.seed_application(application_id=f"app_{cid}", candidate_id=cid)
            session_id = await h.seal_interview(application_id=f"app_{cid}", candidate_id=cid)
            await h.run_evaluation(session_id)

        # Force an exact tie directly on the stored reports (deterministic,
        # provider-independent) rather than relying on the mock LLM to
        # happen to produce identical scores.
        for cid in ("cand_z", "cand_a"):
            application = await h.applications.get(f"app_{cid}")
            job = await h.evaluations.get_for_session(application.session_id)
            tied_scores = job.result.scores.model_copy(update={"weighted_final_score": 8.0})
            tied_report = job.result.model_copy(update={"scores": tied_scores})
            await h.evaluations.save(job.model_copy(update={"result": tied_report}))

        result = await h.recruiter_service.get_leaderboard("job_rec")
        ids_in_rank_order = [e.candidate_id for e in result.leaderboard.entries]
        assert ids_in_rank_order == sorted(ids_in_rank_order)  # candidate_id ascending

    @pytest.mark.asyncio
    async def test_recommendation_filter_narrows_entries_without_changing_summary_counts(self):
        h = RecruiterHarness()
        await h.seed_job()
        await h.seed_candidate("cand_e", "Eve")
        await h.seed_application(application_id="app_e", candidate_id="cand_e")
        session_id = await h.seal_interview(application_id="app_e", candidate_id="cand_e")
        await h.run_evaluation(session_id)

        unfiltered = await h.recruiter_service.get_leaderboard("job_rec")
        real_recommendation = unfiltered.leaderboard.entries[0].recommendation

        matching = await h.recruiter_service.get_leaderboard(
            "job_rec", recommendation=real_recommendation,
        )
        assert len(matching.leaderboard.entries) == 1
        assert matching.leaderboard.total_candidates == unfiltered.leaderboard.total_candidates

        none_matching = await h.recruiter_service.get_leaderboard(
            "job_rec", recommendation="definitely_not_a_real_recommendation",
        )
        assert none_matching.leaderboard.entries == []
        assert none_matching.total_matching == 0

    @pytest.mark.asyncio
    async def test_pagination_slices_entries_without_breaking_ranks(self):
        h = RecruiterHarness()
        await h.seed_job()
        for i in range(3):
            cid = f"cand_p{i}"
            await h.seed_candidate(cid, f"Candidate {i}")
            await h.seed_application(application_id=f"app_p{i}", candidate_id=cid)
            session_id = await h.seal_interview(application_id=f"app_p{i}", candidate_id=cid)
            await h.run_evaluation(session_id)

        page = await h.recruiter_service.get_leaderboard("job_rec", limit=1, offset=1)
        assert len(page.leaderboard.entries) == 1
        assert page.total_matching == 3
        # rank on the returned entry still reflects its position in the
        # FULL ranking, not "1" because it is alone on this page.
        assert page.leaderboard.entries[0].rank == 2

    def test_http_leaderboard_endpoint(self, client, app):
        _seed_http(app, "cand_lb_1", "job_lb_1", "app_lb_1", run_eval=True)
        response = client.get("/jobs/job_lb_1/leaderboard")
        assert response.status_code == 200
        body = response.json()
        assert body["leaderboard"]["job_id"] == "job_lb_1"
        assert len(body["leaderboard"]["entries"]) == 1
        assert body["total_matching"] == 1

    def test_http_leaderboard_unknown_job_is_404(self, client):
        assert client.get("/jobs/never-existed/leaderboard").status_code == 404


# ---------------------------------------------------------------------------
# 14. Human-review flag
# ---------------------------------------------------------------------------

class TestHumanReviewFlag:
    @pytest.mark.asyncio
    async def test_requires_human_review_is_preserved_on_the_entry(self):
        h = RecruiterHarness()
        await h.seed_job()
        await h.seed_candidate("cand_hr", "Human Review Candidate")
        await h.seed_application(application_id="app_hr", candidate_id="cand_hr")
        session_id = await h.seal_interview(application_id="app_hr", candidate_id="cand_hr")
        eval_id = await h.run_evaluation(session_id)

        stored = await h.evaluations.get(eval_id)
        result = await h.recruiter_service.get_leaderboard("job_rec")
        entry = result.leaderboard.entries[0]
        assert entry.requires_human_review == stored.result.requires_human_review

    @pytest.mark.asyncio
    async def test_requires_human_review_filter(self):
        h = RecruiterHarness()
        await h.seed_job()
        await h.seed_candidate("cand_hr2", "Candidate")
        await h.seed_application(application_id="app_hr2", candidate_id="cand_hr2")
        session_id = await h.seal_interview(application_id="app_hr2", candidate_id="cand_hr2")
        await h.run_evaluation(session_id)

        opposite = await h.recruiter_service.get_leaderboard(
            "job_rec", requires_human_review=None,
        )
        entry = opposite.leaderboard.entries[0]
        filtered_out = await h.recruiter_service.get_leaderboard(
            "job_rec", requires_human_review=not entry.requires_human_review,
        )
        assert filtered_out.leaderboard.entries == []


# ---------------------------------------------------------------------------
# 8/9/10. Evaluation states surfaced correctly: pending, running, failed
# ---------------------------------------------------------------------------

class TestEvaluationStatesSurfaced:
    def test_pending_evaluation_is_not_shown_as_completed_anywhere(self, client, app):
        session_id, _ = _seed_http(app, "cand_pend", "job_pend", "app_pend", run_eval="pending")
        via_application = client.get("/applications/app_pend/evaluation").json()
        assert via_application["evaluation"]["status"] == "pending"
        assert via_application["evaluation"]["result"] is None

    def test_failed_evaluation_is_surfaced_with_a_clear_reason_not_a_fake_report(self, client, app):
        _session_id, _ = _seed_http(app, "cand_fail", "job_fail", "app_fail", run_eval="failed")
        response = client.get("/applications/app_fail/evaluation")
        body = response.json()["evaluation"]
        assert body["status"] == "failed"
        assert body["result"] is None
        assert body["error"]

    def test_leaderboard_excludes_a_pending_or_failed_evaluation_from_ranking(self, client, app):
        _seed_http(app, "cand_pend2", "job_pend2", "app_pend2", run_eval="pending")
        response = client.get("/jobs/job_pend2/leaderboard")
        body = response.json()["leaderboard"]
        assert body["entries"] == []
        assert "cand_pend2" in body["incomplete_candidates"]


# ---------------------------------------------------------------------------
# 15. Authorization boundary
# ---------------------------------------------------------------------------

class TestAuthorizationBoundary:
    def test_recruiter_endpoints_work_under_the_default_anonymous_principal(self, client):
        """require_scopes("recruiter:...") is inert while AUTH_ENABLED=false
        (core/security.py) - reaches the real NotFoundError path, proving
        the request was authenticated/authorized as the anonymous
        principal under today's posture, exactly like every other endpoint
        in this backend."""
        assert client.get("/jobs/never-existed/applications").status_code == 404
        assert client.get("/jobs/never-existed/leaderboard").status_code == 404
        assert client.post("/applications/never-existed/shortlist").status_code == 404


# ---------------------------------------------------------------------------
# 20. No sensitive/internal data leakage
# ---------------------------------------------------------------------------

class TestNoDataLeakage:
    def test_leaderboard_and_overview_responses_carry_no_credentials(self, client, app, monkeypatch):
        monkeypatch.setattr("config.settings.GROQ_API_KEY", "sk-leak-check-do-not-expose")
        _seed_http(app, "cand_leak", "job_leak", "app_leak", run_eval=True)
        overview = client.get("/jobs/job_leak/applications")
        leaderboard = client.get("/jobs/job_leak/leaderboard")
        assert "sk-leak-check-do-not-expose" not in overview.text
        assert "sk-leak-check-do-not-expose" not in leaderboard.text

    def test_overview_does_not_embed_the_full_resume_or_full_report(self, client, app):
        """Step 13: the LIST view is narrowed (CandidateSummaryView /
        evaluation_id+status only) - the full ParsedResume/CandidateReport
        stay one call away, not duplicated into every row."""
        _seed_http(app, "cand_narrow", "job_narrow", "app_narrow", run_eval=True)
        response = client.get("/jobs/job_narrow/applications")
        row = response.json()["applications"][0]
        assert "raw_text" not in row["candidate"]
        assert "work_experience" not in row["candidate"]
        assert "result" not in row  # no embedded CandidateReport in the list row


# ---------------------------------------------------------------------------
# HTTP seeding helpers
# ---------------------------------------------------------------------------

def _seed_bare_application(app, *, status: str):
    """Seeds a job/candidate/application (no interview) directly through the
    running app's own container repositories, and returns
    (job_id, candidate_id, application_id)."""
    job_id, candidate_id, application_id = "job_bare", f"cand_bare_{status}", f"app_bare_{status}"

    async def _seed():
        from repositories.interfaces import CandidateRecord, JobRecord

        container = app.state.container
        await container.job_repository.save(JobRecord(job_id=job_id, job=_job(job_id)))
        await container.candidate_repository.save(
            CandidateRecord(candidate_id=candidate_id, user_id="user_anonymous", resume=_resume(candidate_id, "Bare"))
        )
        await container.application_repository.save(Application(
            application_id=application_id, job_id=job_id, candidate_id=candidate_id,
            status=ApplicationStatus(status),
            matching_score=_matching_score(candidate_id, job_id) if status != "submitted" else None,
        ))

    asyncio.run(_seed())
    return job_id, candidate_id, application_id


def _seed_http(app, candidate_id, job_id, application_id, *, run_eval, seal=True):
    """Seeds job/candidate/shortlisted-application through the running
    app's own container, optionally seals an interview and runs (or leaves
    pending/failed) its evaluation - all through the SAME container the
    TestClient serves requests from. Returns (session_id, evaluation_id).

    `run_eval`: True -> run to COMPLETED; "pending" -> trigger, never run;
    "failed" -> force a failure (missing matching score); False -> do not
    even trigger.
    """
    from repositories.interfaces import CandidateRecord, JobRecord

    dispatcher = RecordingDispatcher()
    app.state.container.evaluation_dispatcher = dispatcher

    async def _seed():
        container = app.state.container
        await container.job_repository.save(JobRecord(job_id=job_id, job=_job(job_id)))
        await container.candidate_repository.save(
            CandidateRecord(candidate_id=candidate_id, user_id="user_anonymous", resume=_resume(candidate_id, "HTTP Candidate"))
        )
        await container.application_repository.save(Application(
            application_id=application_id, job_id=job_id, candidate_id=candidate_id,
            status=ApplicationStatus.SHORTLISTED,
            matching_score=None if run_eval == "failed" else _matching_score(candidate_id, job_id),
        ))

        if not seal:
            return None, None

        interview_service = InterviewService(
            registry=app.state.registry, session_repository=container.session_repository,
            transcript_repository=container.transcript_repository,
            interviewer_factory=_scripted_interviewer(),
        )
        session_id, runner = await interview_service.create_session(
            job_description=_job(job_id), parsed_resume=_resume(candidate_id, "HTTP Candidate"),
            candidate_id=candidate_id, max_questions=1, application_id=application_id,
        )
        while not runner.is_finished():
            result = await interview_service.submit_answer(session_id, "A detailed answer.")
            if result.next_question is None:
                break
        application = await container.application_repository.get(application_id)
        await container.application_repository.save(application.model_copy(
            update={"session_id": session_id, "status": ApplicationStatus.INTERVIEW_LINKED}
        ))

        if run_eval is False:
            return session_id, None

        evaluation_service = EvaluationService(
            evaluation_repository=container.evaluation_repository,
            session_repository=container.session_repository,
            transcript_repository=container.transcript_repository,
            application_repository=container.application_repository,
            dispatcher=dispatcher,
        )
        job = await evaluation_service.trigger_evaluation(session_id)
        if run_eval == "pending":
            return session_id, job.evaluation_id
        await dispatcher.run_all()
        return session_id, job.evaluation_id

    return asyncio.run(_seed())


class TestLeaderboardOpeningsSelection:
    @pytest.mark.asyncio
    async def test_leaderboard_selects_exact_number_of_openings(self):
        from agents.leaderboard.agent import LeaderboardAgent
        from schemas.scoring import CandidateReport, CandidateScores

        reports = [
            CandidateReport(
                report_id="rep_1", candidate_id="cand_1", candidate_name="Alice", job_id="job_openings",
                technical_summary="Strong", behavioral_summary="Good", job_fit_summary="Fit",
                explanation="Recommended",
                scores=CandidateScores(
                    score_id="s1", candidate_id="cand_1", job_id="job_openings",
                    technical_score=9.5, behavioral_score=9.0, job_fit_score=9.2, weighted_final_score=9.3,
                    explanation="Top score", confidence=0.95,
                ),
                recommendation="strong_candidate",
            ),
            CandidateReport(
                report_id="rep_2", candidate_id="cand_2", candidate_name="Bob", job_id="job_openings",
                technical_summary="Solid", behavioral_summary="Good", job_fit_summary="Fit",
                explanation="Recommended",
                scores=CandidateScores(
                    score_id="s2", candidate_id="cand_2", job_id="job_openings",
                    technical_score=8.5, behavioral_score=8.0, job_fit_score=8.2, weighted_final_score=8.3,
                    explanation="Solid score", confidence=0.9,
                ),
                recommendation="candidate",
            ),
            CandidateReport(
                report_id="rep_3", candidate_id="cand_3", candidate_name="Charlie", job_id="job_openings",
                technical_summary="Fair", behavioral_summary="Average", job_fit_summary="OK",
                explanation="Under review",
                scores=CandidateScores(
                    score_id="s3", candidate_id="cand_3", job_id="job_openings",
                    technical_score=7.0, behavioral_score=7.5, job_fit_score=7.2, weighted_final_score=7.2,
                    explanation="Average score", confidence=0.85,
                ),
                recommendation="candidate",
            ),
        ]

        agent = LeaderboardAgent()
        result = await agent.execute(reports=reports, job_id="job_openings", openings=2)
        leaderboard = result["leaderboard"]

        assert leaderboard.openings == 2
        assert len(leaderboard.entries) == 3
        assert len(leaderboard.selected_candidates) == 2
        assert leaderboard.selected_candidate_ids == ["cand_1", "cand_2"]

        assert leaderboard.entries[0].candidate_id == "cand_1"
        assert leaderboard.entries[0].is_selected is True
        assert leaderboard.entries[0].selection_status == "selected"

        assert leaderboard.entries[1].candidate_id == "cand_2"
        assert leaderboard.entries[1].is_selected is True
        assert leaderboard.entries[1].selection_status == "selected"

        assert leaderboard.entries[2].candidate_id == "cand_3"
        assert leaderboard.entries[2].is_selected is False
        assert leaderboard.entries[2].selection_status == "waitlisted"

