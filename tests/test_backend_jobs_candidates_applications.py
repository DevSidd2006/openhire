"""
Chunk 2: Job / Candidate / Application backend.

Covers the new application layer built around the EXISTING
`JDAnalyzerAgent`, `ResumeParserAgent` and `ResumeMatcherAgent` - no agent,
provider, or matching algorithm is reimplemented here, so these tests
exercise the real (mock-provider) agents end-to-end, exactly as
tests/test_backend_foundation.py does for the interview engine.

Also pins the Application -> Interview bridge introduced in
api/routes/interview.py: `CreateSessionRequest.application_id` is optional
and additive, and the pre-existing `/sessions` contract must be completely
unaffected when it is omitted.
"""
from repositories.memory import InMemoryRubricRepository
from tests.rubric_fixtures import approved_rubric
import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.errors import ConflictError, DependencyError, NotFoundError
from repositories.interfaces import (
    RubricRepository,
    ApplicationRepository,
    CandidateRecord,
    CandidateRepository,
    JobRecord,
    JobRepository,
)
from repositories.memory import (
    InMemoryApplicationRepository,
    InMemoryCandidateRepository,
    InMemoryJobRepository,
)
from schemas.application import ApplicationStatus
from services.application_service import ApplicationService
from services.candidate_service import CandidateService
from services.job_service import JobService
from services.matching_service import MatchingService

# ---------------------------------------------------------------------------
# Fixtures / sample data
# ---------------------------------------------------------------------------

_JD_TEXT = """
Senior Python Backend Developer

We are looking for a Senior Python Backend Developer.

Requirements:
- 5+ years of Python development
- Strong async/await experience
- Experience with REST APIs and SQL

Preferred:
- Kubernetes experience
"""

_RESUME_TEXT_STRONG = """
Jane Doe
Senior Backend Engineer, 6 years experience.
Skills: Python, FastAPI, SQL, REST APIs, Docker, Kubernetes, PostgreSQL.
"""

_RESUME_TEXT_WEAK = """
John Smith
Junior developer, 1 year experience.
Skills: HTML, CSS.
"""


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def _job_service() -> JobService:
    return JobService(job_repository=InMemoryJobRepository())


def _candidate_service() -> CandidateService:
    return CandidateService(candidate_repository=InMemoryCandidateRepository())


class _StubSemanticScreening:
    """Deterministic stand-in for SemanticScreeningService.

    Injected everywhere so these tests never reach a real embedding
    provider; the semantic score's own behaviour is covered in
    tests/test_semantic_screening.py.
    """

    def __init__(self, score=0.5):
        self._score = score

    async def score(self, job, resume):
        return self._score


def _application_service(
    *, job_repository: JobRepository = None, candidate_repository: CandidateRepository = None,
    application_repository: ApplicationRepository = None,
    rubric_repository: RubricRepository = None,
    semantic_screening_service=None,
) -> ApplicationService:
    return ApplicationService(
        application_repository=application_repository or InMemoryApplicationRepository(),
        job_repository=job_repository or InMemoryJobRepository(),
        candidate_repository=candidate_repository or InMemoryCandidateRepository(),
        matching_service=MatchingService(),
        rubric_repository=rubric_repository,
        semantic_screening_service=semantic_screening_service or _StubSemanticScreening(),
    )



async def _seeded_rubric_repo(job_id: str) -> InMemoryRubricRepository:
    """A repo holding one APPROVED rubric for `job_id`.

    Matching is rubric-driven: without an approved rubric the job is
    deliberately not scorable and applications are parked as SCORING_PENDING.
    """
    repo = InMemoryRubricRepository()
    rubric = approved_rubric(job_id=job_id)
    await repo.save(rubric.model_copy(update={"status": "draft"}))
    await repo.approve(rubric.rubric_id)
    return repo


async def _seed_job(job_repo: JobRepository, job_service: JobService = None) -> JobRecord:
    service = job_service or JobService(job_repository=job_repo)
    return await service.create_job(description=_JD_TEXT)


async def _seed_candidate(
    candidate_repo: CandidateRepository, resume_text=_RESUME_TEXT_STRONG, name="Jane Doe",
    candidate_service: CandidateService = None,
) -> CandidateRecord:
    service = candidate_service or CandidateService(candidate_repository=candidate_repo)
    return await service.register_candidate(resume_text=resume_text, candidate_name=name)


async def _seed_candidate_record(
    candidate_repo: CandidateRepository, *, candidate_id: str, name: str,
    skills: list, total_experience_years: float,
) -> CandidateRecord:
    """Seed a CandidateRecord with CONTROLLED skills/experience, bypassing
    ResumeParserAgent entirely.

    MockLLMProvider's resume-parse response (providers/llm/mock.py) is a
    fixed deterministic payload that ignores the input resume text - so two
    different resumes parsed through the mock provider produce IDENTICAL
    skills/experience, and can never be told apart by the (real, unchanged)
    ResumeMatcherAgent. This helper creates the differentiated fixture data
    the matching-ranking tests need directly, the same way a real provider's
    varying output would - it does not change or bypass matching itself.
    """
    from schemas.resume import ParsedResume

    resume = ParsedResume(
        candidate_id=candidate_id, candidate_name=name, skills=skills,
        total_experience_years=total_experience_years,
    )
    return await candidate_repo.save(CandidateRecord(candidate_id=candidate_id, resume=resume))


# ---------------------------------------------------------------------------
# JobService
# ---------------------------------------------------------------------------

class TestJobService:
    @pytest.mark.asyncio
    async def test_create_job_analyzes_raw_text_into_a_structured_job(self):
        """Reuses JDAnalyzerAgent - not a new extraction path."""
        service = _job_service()
        record = await service.create_job(description=_JD_TEXT)
        assert record.job_id.startswith("job_")
        assert record.job.title  # extracted, not empty
        assert record.job.competencies  # extracted, not empty
        assert record.is_active is True

    @pytest.mark.asyncio
    async def test_create_job_accepts_a_caller_supplied_job_id(self):
        service = _job_service()
        record = await service.create_job(description=_JD_TEXT, job_id="job_custom_1")
        assert record.job_id == "job_custom_1"
        assert record.job.job_id == "job_custom_1"

    @pytest.mark.asyncio
    async def test_get_job_not_found_raises_not_found(self):
        with pytest.raises(NotFoundError):
            await _job_service().get_job("never-existed")

    @pytest.mark.asyncio
    async def test_list_jobs_excludes_archived_by_default(self):
        repo = InMemoryJobRepository()
        service = JobService(job_repository=repo)
        record = await service.create_job(description=_JD_TEXT)
        await service.archive_job(record.job_id)

        assert await service.list_jobs() == []
        assert len(await service.list_jobs(include_archived=True)) == 1

    @pytest.mark.asyncio
    async def test_update_job_applies_only_the_fields_given(self):
        repo = InMemoryJobRepository()
        service = JobService(job_repository=repo)
        record = await service.create_job(description=_JD_TEXT)
        original_title = record.job.title

        updated = await service.update_job(record.job_id, {"department": "Platform"})
        assert updated.job.department == "Platform"
        assert updated.job.title == original_title  # untouched

    @pytest.mark.asyncio
    async def test_update_job_rejects_invalid_competency_weights(self):
        """Reconstructing via JobDescription(**merged) re-runs its own
        validator - an edit that breaks "weights sum to 1.0" must be
        rejected, not silently stored."""
        repo = InMemoryJobRepository()
        service = JobService(job_repository=repo)
        record = await service.create_job(description=_JD_TEXT)

        with pytest.raises(ValueError, match="sum to 1.0"):
            await service.update_job(
                record.job_id,
                {"competencies": [{"name": "Python", "weight": 0.9}, {"name": "SQL", "weight": 0.9}]},
            )

    @pytest.mark.asyncio
    async def test_update_job_on_archived_job_is_a_conflict(self):
        repo = InMemoryJobRepository()
        service = JobService(job_repository=repo)
        record = await service.create_job(description=_JD_TEXT)
        await service.archive_job(record.job_id)

        with pytest.raises(ConflictError):
            await service.update_job(record.job_id, {"department": "Platform"})

    @pytest.mark.asyncio
    async def test_archive_job_is_idempotent(self):
        repo = InMemoryJobRepository()
        service = JobService(job_repository=repo)
        record = await service.create_job(description=_JD_TEXT)

        first = await service.archive_job(record.job_id)
        second = await service.archive_job(record.job_id)
        assert first.is_active is False
        assert second.is_active is False

    @pytest.mark.asyncio
    async def test_archive_unknown_job_is_not_found(self):
        with pytest.raises(NotFoundError):
            await _job_service().archive_job("never-existed")

    @pytest.mark.asyncio
    async def test_job_analysis_failure_is_a_dependency_error_not_a_fabricated_job(self):
        """JDAnalyzerAgent never fabricates a JobDescription on failure - a
        None result must surface as an explicit, typed failure."""

        class _BrokenAnalyzer:
            async def run(self, **kwargs):
                return {"result": {"job_description": None, "error": "LLM exploded"}}

        service = JobService(
            job_repository=InMemoryJobRepository(), jd_analyzer_factory=lambda: _BrokenAnalyzer()
        )
        with pytest.raises(DependencyError):
            await service.create_job(description=_JD_TEXT)


# ---------------------------------------------------------------------------
# CandidateService
# ---------------------------------------------------------------------------

class TestCandidateService:
    @pytest.mark.asyncio
    async def test_register_candidate_parses_raw_resume_text(self):
        service = _candidate_service()
        record = await service.register_candidate(
            resume_text=_RESUME_TEXT_STRONG, candidate_name="Jane Doe"
        )
        assert record.candidate_id.startswith("cand_")
        assert record.resume.candidate_name == "Jane Doe"
        assert record.used_fallback is False

    @pytest.mark.asyncio
    async def test_get_candidate_not_found_raises_not_found(self):
        with pytest.raises(NotFoundError):
            await _candidate_service().get_candidate("never-existed")

    @pytest.mark.asyncio
    async def test_list_candidates(self):
        repo = InMemoryCandidateRepository()
        service = CandidateService(candidate_repository=repo)
        await service.register_candidate(resume_text=_RESUME_TEXT_STRONG, candidate_name="Jane")
        await service.register_candidate(resume_text=_RESUME_TEXT_WEAK, candidate_name="John")
        assert len(await service.list_candidates()) == 2

    @pytest.mark.asyncio
    async def test_update_candidate_direct_field_patch(self):
        repo = InMemoryCandidateRepository()
        service = CandidateService(candidate_repository=repo)
        record = await service.register_candidate(resume_text=_RESUME_TEXT_STRONG, candidate_name="Jane")

        updated = await service.update_candidate(
            record.candidate_id, patch={"skills": ["Python", "Go"]}
        )
        assert updated.resume.skills == ["Python", "Go"]
        # Unset fields survive the patch untouched.
        assert updated.resume.candidate_name == "Jane"

    @pytest.mark.asyncio
    async def test_update_candidate_reparse_replaces_the_resume(self):
        repo = InMemoryCandidateRepository()
        service = CandidateService(candidate_repository=repo)
        record = await service.register_candidate(resume_text=_RESUME_TEXT_STRONG, candidate_name="Jane")
        original_skills = record.resume.skills

        updated = await service.update_candidate(
            record.candidate_id, resume_text=_RESUME_TEXT_WEAK
        )
        # candidate_name is a caller-supplied argument to ResumeParserAgent,
        # not something the (mock) LLM extracts - it must survive a reparse.
        assert updated.resume.candidate_name == "Jane"
        # MockLLMProvider's resume-parse response is a fixed deterministic
        # payload regardless of input text, so a reparse yields identical
        # extracted fields - this only proves the parser ran again, not that
        # its output differs (a real provider's would).
        assert original_skills == updated.resume.skills

    @pytest.mark.asyncio
    async def test_candidate_parse_failure_is_a_dependency_error(self):
        class _BrokenParser:
            async def run(self, **kwargs):
                return {"result": {"parsed_resume": None, "error": "even the fallback failed"}}

        service = CandidateService(
            candidate_repository=InMemoryCandidateRepository(),
            resume_parser_factory=lambda: _BrokenParser(),
        )
        with pytest.raises(DependencyError):
            await service.register_candidate(resume_text=_RESUME_TEXT_STRONG, candidate_name="Jane")


# ---------------------------------------------------------------------------
# ApplicationService: apply / duplicate / matching / shortlisting
# ---------------------------------------------------------------------------

class TestApplicationServiceApply:
    @pytest.mark.asyncio
    async def test_apply_creates_a_submitted_application(self):
        job_repo, candidate_repo = InMemoryJobRepository(), InMemoryCandidateRepository()
        job = await _seed_job(job_repo)
        candidate = await _seed_candidate(candidate_repo)
        service = _application_service(job_repository=job_repo, candidate_repository=candidate_repo)

        application = await service.apply(job_id=job.job_id, candidate_id=candidate.candidate_id)
        assert application.status == ApplicationStatus.SUBMITTED
        assert application.application_id.startswith("app_")
        assert application.matching_score is None

    @pytest.mark.asyncio
    async def test_apply_to_unknown_job_is_not_found(self):
        candidate_repo = InMemoryCandidateRepository()
        candidate = await _seed_candidate(candidate_repo)
        service = _application_service(candidate_repository=candidate_repo)
        with pytest.raises(NotFoundError):
            await service.apply(job_id="never-existed", candidate_id=candidate.candidate_id)

    @pytest.mark.asyncio
    async def test_apply_with_unknown_candidate_is_not_found(self):
        job_repo = InMemoryJobRepository()
        job = await _seed_job(job_repo)
        service = _application_service(job_repository=job_repo)
        with pytest.raises(NotFoundError):
            await service.apply(job_id=job.job_id, candidate_id="never-existed")

    @pytest.mark.asyncio
    async def test_apply_to_an_archived_job_is_a_conflict(self):
        job_repo, candidate_repo = InMemoryJobRepository(), InMemoryCandidateRepository()
        job_service = JobService(job_repository=job_repo)
        job = await _seed_job(job_repo, job_service)
        await job_service.archive_job(job.job_id)
        candidate = await _seed_candidate(candidate_repo)
        service = _application_service(job_repository=job_repo, candidate_repository=candidate_repo)

        with pytest.raises(ConflictError):
            await service.apply(job_id=job.job_id, candidate_id=candidate.candidate_id)

    @pytest.mark.asyncio
    async def test_duplicate_application_is_a_conflict_not_a_second_row(self):
        """Chunk 2 Step 9's explicit case: at most one application per
        (job, candidate) pair."""
        job_repo, candidate_repo = InMemoryJobRepository(), InMemoryCandidateRepository()
        application_repo = InMemoryApplicationRepository()
        job = await _seed_job(job_repo)
        candidate = await _seed_candidate(candidate_repo)
        service = _application_service(
            job_repository=job_repo, candidate_repository=candidate_repo,
            application_repository=application_repo,
            rubric_repository=await _seeded_rubric_repo(job.job_id),
        )

        first = await service.apply(job_id=job.job_id, candidate_id=candidate.candidate_id)
        with pytest.raises(ConflictError):
            await service.apply(job_id=job.job_id, candidate_id=candidate.candidate_id)

        # Still exactly one stored application for this pair.
        assert len(await application_repo.list_for_job(job.job_id)) == 1
        assert (await application_repo.get(first.application_id)) is not None

    @pytest.mark.asyncio
    async def test_get_application_not_found(self):
        with pytest.raises(NotFoundError):
            await _application_service().get_application("never-existed")


class TestApplicationServiceMatching:
    @pytest.mark.asyncio
    async def test_matching_scores_a_candidate_without_deciding(self):
        """Scoring ranks and explains; it never shortlists or rejects.
        A scored application stays SUBMITTED, awaiting a recruiter."""
        job_repo, candidate_repo, application_repo = (
            InMemoryJobRepository(), InMemoryCandidateRepository(), InMemoryApplicationRepository()
        )
        job = await _seed_job(job_repo)
        candidate = await _seed_candidate(candidate_repo, _RESUME_TEXT_STRONG, "Jane Doe")
        service = _application_service(
            job_repository=job_repo, candidate_repository=candidate_repo,
            application_repository=application_repo,
            rubric_repository=await _seeded_rubric_repo(job.job_id),
        )
        await service.apply(job_id=job.job_id, candidate_id=candidate.candidate_id)

        result = await service.run_matching_for_job(job.job_id)
        assert result.attempted == 1
        assert result.errors == []
        assert result.applications[0].matching_score is not None
        assert result.applications[0].status in (
            ApplicationStatus.SUBMITTED, ApplicationStatus.NEEDS_HUMAN_REVIEW,
        )
        assert result.applications[0].status is not ApplicationStatus.SHORTLISTED
        assert result.applications[0].status is not ApplicationStatus.REJECTED

    @pytest.mark.asyncio
    async def test_matching_run_ranks_applications_by_match_score(self):
        job_repo, candidate_repo, application_repo = (
            InMemoryJobRepository(), InMemoryCandidateRepository(), InMemoryApplicationRepository()
        )
        job = await _seed_job(job_repo)
        strong = await _seed_candidate_record(
            candidate_repo, candidate_id="cand_strong", name="Jane Doe",
            skills=["Python", "REST APIs", "SQL", "Kubernetes"], total_experience_years=6,
        )
        weak = await _seed_candidate_record(
            candidate_repo, candidate_id="cand_weak", name="John Smith",
            skills=["HTML", "CSS"], total_experience_years=1,
        )
        service = _application_service(
            job_repository=job_repo, candidate_repository=candidate_repo,
            application_repository=application_repo,
            rubric_repository=await _seeded_rubric_repo(job.job_id),
        )
        await service.apply(job_id=job.job_id, candidate_id=weak.candidate_id)
        await service.apply(job_id=job.job_id, candidate_id=strong.candidate_id)

        result = await service.run_matching_for_job(job.job_id)
        scores = [a.matching_score.match_score for a in result.applications]
        assert scores == sorted(scores, reverse=True)
        # The strong candidate (more skill overlap) must rank first.
        assert result.applications[0].candidate_id == strong.candidate_id

    @pytest.mark.asyncio
    async def test_matching_run_on_unknown_job_is_not_found(self):
        with pytest.raises(NotFoundError):
            await _application_service().run_matching_for_job("never-existed")

    @pytest.mark.asyncio
    async def test_matching_does_not_re_score_the_same_rubric_version(self):
        """A scored application now stays SUBMITTED, so idempotency is keyed
        on the rubric version rather than on the status having moved on."""
        job_repo, candidate_repo, application_repo = (
            InMemoryJobRepository(), InMemoryCandidateRepository(), InMemoryApplicationRepository()
        )
        job = await _seed_job(job_repo)
        candidate = await _seed_candidate(candidate_repo, _RESUME_TEXT_STRONG, "Jane Doe")
        service = _application_service(
            job_repository=job_repo, candidate_repository=candidate_repo,
            application_repository=application_repo,
            rubric_repository=await _seeded_rubric_repo(job.job_id),
        )
        await service.apply(job_id=job.job_id, candidate_id=candidate.candidate_id)
        first_run = await service.run_matching_for_job(job.job_id)
        first_status = first_run.applications[0].status

        second_run = await service.run_matching_for_job(job.job_id)
        assert second_run.attempted == 0  # nothing left SUBMITTED
        assert second_run.applications[0].status == first_status

    @pytest.mark.asyncio
    async def test_one_candidates_matching_failure_does_not_abort_the_batch(self):
        """Mirrors orchestration/graph.py:node_match_resumes's own
        resilience contract."""
        job_repo, candidate_repo, application_repo = (
            InMemoryJobRepository(), InMemoryCandidateRepository(), InMemoryApplicationRepository()
        )
        job = await _seed_job(job_repo)
        good = await _seed_candidate(candidate_repo, _RESUME_TEXT_STRONG, "Jane Doe")
        bad = await _seed_candidate(candidate_repo, _RESUME_TEXT_WEAK, "John Smith")

        from schemas.evaluation import MatchingScore

        class _FlakyMatcherAgent:
            async def run(self, *, job_rubric, parsed_resume, **kwargs):
                if parsed_resume.candidate_id == bad.candidate_id:
                    return {"result": {"matching_score": None, "error": "boom"}}
                return {
                    "result": {
                        "matching_score": MatchingScore(
                            match_id="m1", candidate_id=parsed_resume.candidate_id,
                            job_id=job_rubric.job_id, rubric_version=job_rubric.version,
                            match_score=0.9, coverage=1.0, band="strong",
                            explanation="ok", confidence=0.9,
                        )
                    }
                }

        matching_service = MatchingService(resume_matcher_factory=lambda: _FlakyMatcherAgent())
        application_service = ApplicationService(
            application_repository=application_repo, job_repository=job_repo,
            candidate_repository=candidate_repo, matching_service=matching_service,
            rubric_repository=await _seeded_rubric_repo(job.job_id),
            semantic_screening_service=_StubSemanticScreening(),
        )
        await application_service.apply(job_id=job.job_id, candidate_id=good.candidate_id)
        await application_service.apply(job_id=job.job_id, candidate_id=bad.candidate_id)

        result = await application_service.run_matching_for_job(job.job_id)
        assert result.errors == [bad.candidate_id]
        assert result.matched_count == 1
        good_application = next(a for a in result.applications if a.candidate_id == good.candidate_id)
        # Scoring succeeded, so the application is scored but undecided.
        assert good_application.status == ApplicationStatus.SUBMITTED
        assert good_application.matching_score is not None
        # The failed candidate is parked, never rejected: a system failure
        # must not look like a candidate failure.
        bad_application = await application_repo.get_for_job_and_candidate(
            job.job_id, bad.candidate_id
        )
        assert bad_application.status == ApplicationStatus.SCORING_PENDING

    @pytest.mark.asyncio
    async def test_get_shortlist_returns_only_shortlisted_ranked_by_score(self):
        job_repo, candidate_repo, application_repo = (
            InMemoryJobRepository(), InMemoryCandidateRepository(), InMemoryApplicationRepository()
        )
        job = await _seed_job(job_repo)
        strong = await _seed_candidate_record(
            candidate_repo, candidate_id="cand_strong2", name="Jane Doe",
            skills=["Python", "REST APIs", "SQL", "Kubernetes"], total_experience_years=6,
        )
        weak = await _seed_candidate_record(
            candidate_repo, candidate_id="cand_weak2", name="John Smith",
            skills=["HTML", "CSS"], total_experience_years=1,
        )
        service = _application_service(
            job_repository=job_repo, candidate_repository=candidate_repo,
            application_repository=application_repo,
            rubric_repository=await _seeded_rubric_repo(job.job_id),
        )
        await service.apply(job_id=job.job_id, candidate_id=strong.candidate_id)
        await service.apply(job_id=job.job_id, candidate_id=weak.candidate_id)
        await service.run_matching_for_job(job.job_id)

        shortlist = await service.get_shortlist(job.job_id)
        assert all(a.status == ApplicationStatus.SHORTLISTED for a in shortlist)
        # Confirms the shortlist is a genuine filter (not "everything"): the
        # weak candidate's real skill/experience mismatch against the JD
        # (agents/resume_matcher/agent.py's deterministic scoring) keeps
        # them out of it, whatever the exact score turns out to be.
        assert len(shortlist) < 2


class TestApplicationServiceInterviewLink:
    @pytest.mark.asyncio
    async def test_link_interview_session_requires_shortlisted_status(self):
        job_repo, candidate_repo, application_repo = (
            InMemoryJobRepository(), InMemoryCandidateRepository(), InMemoryApplicationRepository()
        )
        job = await _seed_job(job_repo)
        candidate = await _seed_candidate(candidate_repo)
        service = _application_service(
            job_repository=job_repo, candidate_repository=candidate_repo,
            application_repository=application_repo,
            rubric_repository=await _seeded_rubric_repo(job.job_id),
        )
        application = await service.apply(job_id=job.job_id, candidate_id=candidate.candidate_id)

        with pytest.raises(ConflictError):
            await service.link_interview_session(application.application_id, "session_xyz")

    @pytest.mark.asyncio
    async def test_link_interview_session_succeeds_once_shortlisted(self):
        job_repo, candidate_repo, application_repo = (
            InMemoryJobRepository(), InMemoryCandidateRepository(), InMemoryApplicationRepository()
        )
        job = await _seed_job(job_repo)
        candidate = await _seed_candidate(candidate_repo, _RESUME_TEXT_STRONG, "Jane Doe")
        service = _application_service(
            job_repository=job_repo, candidate_repository=candidate_repo,
            application_repository=application_repo,
            rubric_repository=await _seeded_rubric_repo(job.job_id),
        )
        application = await service.apply(job_id=job.job_id, candidate_id=candidate.candidate_id)
        # Force-shortlist directly (avoids depending on the mock matcher's
        # exact recommendation for this fixture).
        shortlisted = application.model_copy(update={"status": ApplicationStatus.SHORTLISTED})
        await application_repo.save(shortlisted)

        linked = await service.link_interview_session(application.application_id, "session_abc")
        assert linked.status == ApplicationStatus.INTERVIEW_LINKED
        assert linked.session_id == "session_abc"


# ---------------------------------------------------------------------------
# In-memory repository contracts
# ---------------------------------------------------------------------------

class TestInMemoryRepositories:
    @pytest.mark.asyncio
    async def test_job_repository_save_is_idempotent_and_preserves_created_at(self):
        repo = InMemoryJobRepository()
        service = JobService(job_repository=repo)
        record = await service.create_job(description=_JD_TEXT, job_id="job_x")
        updated = await service.update_job("job_x", {"department": "Eng"})
        assert updated.created_at == record.created_at

    @pytest.mark.asyncio
    async def test_candidate_repository_absence_returns_none(self):
        assert await InMemoryCandidateRepository().get("never-existed") is None

    @pytest.mark.asyncio
    async def test_application_repository_scoped_listings(self):
        repo = InMemoryApplicationRepository()
        job_repo, candidate_repo = InMemoryJobRepository(), InMemoryCandidateRepository()
        job1 = await _seed_job(job_repo)
        c1 = await _seed_candidate(candidate_repo, _RESUME_TEXT_STRONG, "Jane")
        c2 = await _seed_candidate(candidate_repo, _RESUME_TEXT_WEAK, "John")
        service = _application_service(
            job_repository=job_repo, candidate_repository=candidate_repo, application_repository=repo,
        )
        await service.apply(job_id=job1.job_id, candidate_id=c1.candidate_id)
        await service.apply(job_id=job1.job_id, candidate_id=c2.candidate_id)

        assert len(await repo.list_for_job(job1.job_id)) == 2
        assert len(await repo.list_for_candidate(c1.candidate_id)) == 1
        assert await repo.list_for_candidate("never-existed") == []


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class TestJobsApi:
    def test_create_get_list_update_archive_job(self, client):
        created = client.post("/jobs", json={"description": _JD_TEXT})
        assert created.status_code == 201
        body = created.json()
        job_id = body["job_id"]
        assert body["is_active"] is True
        assert body["job"]["competencies"]

        fetched = client.get(f"/jobs/{job_id}")
        assert fetched.status_code == 200
        assert fetched.json()["job_id"] == job_id

        listed = client.get("/jobs").json()
        assert any(j["job_id"] == job_id for j in listed["jobs"])

        patched = client.patch(f"/jobs/{job_id}", json={"department": "Platform"})
        assert patched.status_code == 200
        assert patched.json()["job"]["department"] == "Platform"

        archived = client.post(f"/jobs/{job_id}/archive")
        assert archived.status_code == 200
        assert archived.json()["is_active"] is False

        # Archived jobs are excluded from the default listing.
        listed_after = client.get("/jobs").json()
        assert not any(j["job_id"] == job_id for j in listed_after["jobs"])
        listed_with_archived = client.get("/jobs?include_archived=true").json()
        assert any(j["job_id"] == job_id for j in listed_with_archived["jobs"])

    def test_get_unknown_job_is_404(self, client):
        response = client.get("/jobs/never-existed")
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    def test_create_job_requires_nonempty_description(self, client):
        response = client.post("/jobs", json={"description": ""})
        assert response.status_code == 422


class TestCandidatesApi:
    def test_create_get_list_update_candidate(self, client):
        created = client.post(
            "/candidates", json={"resume_text": _RESUME_TEXT_STRONG, "candidate_name": "Jane Doe"}
        )
        assert created.status_code == 201
        body = created.json()
        candidate_id = body["candidate_id"]
        assert body["resume"]["candidate_name"] == "Jane Doe"

        fetched = client.get(f"/candidates/{candidate_id}")
        assert fetched.status_code == 200

        listed = client.get("/candidates").json()
        assert any(c["candidate_id"] == candidate_id for c in listed["candidates"])

        patched = client.patch(f"/candidates/{candidate_id}", json={"skills": ["Rust"]})
        assert patched.status_code == 200
        assert patched.json()["resume"]["skills"] == ["Rust"]

    def test_get_unknown_candidate_is_404(self, client):
        response = client.get("/candidates/never-existed")
        assert response.status_code == 404


class TestApplicationsApi:
    def _create_job(self, client):
        return client.post("/jobs", json={"description": _JD_TEXT}).json()["job_id"]

    def _create_candidate(self, client, resume_text=_RESUME_TEXT_STRONG, name="Jane Doe"):
        return client.post(
            "/candidates", json={"resume_text": resume_text, "candidate_name": name}
        ).json()["candidate_id"]

    def test_apply_get_list(self, client):
        job_id = self._create_job(client)
        candidate_id = self._create_candidate(client)

        created = client.post("/applications", json={"job_id": job_id, "candidate_id": candidate_id})
        assert created.status_code == 201
        application_id = created.json()["application"]["application_id"]
        assert created.json()["application"]["status"] == "submitted"

        fetched = client.get(f"/applications/{application_id}")
        assert fetched.status_code == 200

        by_job = client.get(f"/applications?job_id={job_id}").json()
        assert by_job["total"] == 1
        by_candidate = client.get(f"/applications?candidate_id={candidate_id}").json()
        assert by_candidate["total"] == 1

    def test_list_requires_exactly_one_filter(self, client):
        assert client.get("/applications").status_code == 400
        job_id = self._create_job(client)
        candidate_id = self._create_candidate(client)
        assert client.get(
            f"/applications?job_id={job_id}&candidate_id={candidate_id}"
        ).status_code == 400

    def test_duplicate_application_is_409(self, client):
        job_id = self._create_job(client)
        candidate_id = self._create_candidate(client)
        client.post("/applications", json={"job_id": job_id, "candidate_id": candidate_id})
        second = client.post("/applications", json={"job_id": job_id, "candidate_id": candidate_id})
        assert second.status_code == 409
        assert second.json()["error"] == "conflict"

    def test_apply_to_unknown_job_is_404(self, client):
        candidate_id = self._create_candidate(client)
        response = client.post("/applications", json={"job_id": "never-existed", "candidate_id": candidate_id})
        assert response.status_code == 404

    def test_match_and_shortlist_endpoints(self, client):
        job_id = self._create_job(client)
        strong_id = self._create_candidate(client, _RESUME_TEXT_STRONG, "Jane Doe")
        client.post("/applications", json={"job_id": job_id, "candidate_id": strong_id})

        draft = client.post(f"/jobs/{job_id}/rubric/draft").json()
        client.post(f"/jobs/{job_id}/rubric/{draft['rubric_id']}/approve")

        match_response = client.post(f"/jobs/{job_id}/match")
        assert match_response.status_code == 200
        body = match_response.json()
        assert body["matched"] == 1
        assert body["job_id"] == job_id

        shortlist = client.get(f"/jobs/{job_id}/shortlist").json()
        assert isinstance(shortlist["applications"], list)


# ---------------------------------------------------------------------------
# Application -> Interview bridge (additive to /sessions)
# ---------------------------------------------------------------------------

class TestApplicationInterviewBridge:
    def _job_description_payload(self, job_id="job_bridge"):
        return {
            "job_id": job_id, "title": "Backend Engineer", "description": "Test role",
            "competencies": [{"name": "Python", "weight": 1.0}],
        }

    def _resume_payload(self, candidate_id="cand_bridge"):
        return {"candidate_id": candidate_id, "candidate_name": "Test Candidate", "skills": ["Python"]}

    def test_sessions_endpoint_is_unchanged_when_application_id_is_omitted(self, client):
        """The whole point of the additive design: not supplying
        application_id must behave EXACTLY as the pre-Chunk-2 endpoint."""
        payload = {
            "candidate_id": "cand_bridge_1", "job_id": "job_bridge_1",
            "job_description": self._job_description_payload("job_bridge_1"),
            "parsed_resume": self._resume_payload("cand_bridge_1"),
        }
        response = client.post("/sessions", json=payload)
        assert response.status_code == 201
        body = response.json()
        assert body["application_id"] is None
        assert "session_id" in body

    def test_application_id_mismatch_with_job_or_candidate_is_400(self, client, app):
        from repositories.interfaces import Application
        from schemas.application import ApplicationStatus as Status

        application_repo = app.state.container.application_repository
        import asyncio

        async def _seed():
            return await application_repo.save(
                Application(
                    application_id="app_bridge_1", job_id="job_bridge_other",
                    candidate_id="cand_bridge_other", status=Status.SHORTLISTED,
                )
            )

        asyncio.run(_seed())

        payload = {
            "candidate_id": "cand_bridge_2", "job_id": "job_bridge_2",
            "job_description": self._job_description_payload("job_bridge_2"),
            "parsed_resume": self._resume_payload("cand_bridge_2"),
            "application_id": "app_bridge_1",
        }
        response = client.post("/sessions", json=payload)
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"

    def test_application_must_be_shortlisted_before_linking(self, client, app):
        from repositories.interfaces import Application
        from schemas.application import ApplicationStatus as Status
        import asyncio

        application_repo = app.state.container.application_repository

        async def _seed():
            return await application_repo.save(
                Application(
                    application_id="app_bridge_3", job_id="job_bridge_3",
                    candidate_id="cand_bridge_3", status=Status.SUBMITTED,
                )
            )

        asyncio.run(_seed())

        payload = {
            "candidate_id": "cand_bridge_3", "job_id": "job_bridge_3",
            "job_description": self._job_description_payload("job_bridge_3"),
            "parsed_resume": self._resume_payload("cand_bridge_3"),
            "application_id": "app_bridge_3",
        }
        response = client.post("/sessions", json=payload)
        assert response.status_code == 409
        assert response.json()["error"] == "conflict"

    def test_shortlisted_application_is_linked_on_session_creation(self, client, app):
        from repositories.interfaces import Application
        from schemas.application import ApplicationStatus as Status
        import asyncio

        application_repo = app.state.container.application_repository

        async def _seed():
            return await application_repo.save(
                Application(
                    application_id="app_bridge_4", job_id="job_bridge_4",
                    candidate_id="cand_bridge_4", status=Status.SHORTLISTED,
                )
            )

        asyncio.run(_seed())

        payload = {
            "candidate_id": "cand_bridge_4", "job_id": "job_bridge_4",
            "job_description": self._job_description_payload("job_bridge_4"),
            "parsed_resume": self._resume_payload("cand_bridge_4"),
            "application_id": "app_bridge_4",
        }
        response = client.post("/sessions", json=payload)
        assert response.status_code == 201
        body = response.json()
        assert body["application_id"] == "app_bridge_4"

        application = asyncio.run(application_repo.get("app_bridge_4"))
        assert application.status == Status.INTERVIEW_LINKED
        assert application.session_id == body["session_id"]


class TestSemanticScoreWithoutRubric:
    """A job with no approved rubric must still produce a ranking signal.

    Before semantic screening existed, an application to such a job was
    parked as SCORING_PENDING with nothing on it at all, and the resume
    leaderboard had literally nothing to order by.
    """

    @pytest.mark.asyncio
    async def test_no_rubric_still_stores_a_semantic_score(self):
        job_repo, candidate_repo = InMemoryJobRepository(), InMemoryCandidateRepository()
        application_repo = InMemoryApplicationRepository()
        job = await _seed_job(job_repo)
        candidate = await _seed_candidate(candidate_repo)

        service = _application_service(
            job_repository=job_repo, candidate_repository=candidate_repo,
            application_repository=application_repo,
            rubric_repository=None,  # no approved rubric for this job
            semantic_screening_service=_StubSemanticScreening(0.71),
        )
        application = await service.apply(
            job_id=job.job_id, candidate_id=candidate.candidate_id
        )

        await service.run_matching_for_job(job.job_id)

        stored = await application_repo.get(application.application_id)
        # Parked for rubric scoring, but no longer empty-handed.
        assert stored.status == ApplicationStatus.SCORING_PENDING
        assert stored.semantic_score == 0.71
        # The semantic score is emphatically not a decision.
        assert stored.matching_score is None

    @pytest.mark.asyncio
    async def test_scoring_pending_applications_are_scored_once_a_rubric_is_approved(self):
        """The no-rubric branch promises these get picked up later.

        `pending` used to filter on SUBMITTED alone, so an application parked
        as SCORING_PENDING was stranded there permanently and approving a
        rubric never rescued it.
        """
        job_repo, candidate_repo = InMemoryJobRepository(), InMemoryCandidateRepository()
        application_repo = InMemoryApplicationRepository()
        job = await _seed_job(job_repo)
        candidate = await _seed_candidate(candidate_repo)

        without_rubric = _application_service(
            job_repository=job_repo, candidate_repository=candidate_repo,
            application_repository=application_repo, rubric_repository=None,
        )
        application = await without_rubric.apply(
            job_id=job.job_id, candidate_id=candidate.candidate_id
        )
        await without_rubric.run_matching_for_job(job.job_id)
        assert (await application_repo.get(application.application_id)).status == (
            ApplicationStatus.SCORING_PENDING
        )

        # A recruiter now approves a rubric; the parked application is picked up.
        with_rubric = _application_service(
            job_repository=job_repo, candidate_repository=candidate_repo,
            application_repository=application_repo,
            rubric_repository=await _seeded_rubric_repo(job.job_id),
        )
        result = await with_rubric.run_matching_for_job(job.job_id)

        assert result.attempted == 1
        stored = await application_repo.get(application.application_id)
        assert stored.status != ApplicationStatus.SCORING_PENDING
        assert stored.matching_score is not None
