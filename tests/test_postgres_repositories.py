"""
Database chunk: contract tests for repositories/postgres against a REAL
PostgreSQL database.

Mirrors the two-tier testing philosophy the project already uses for real
LLM providers (LLM_PROVIDER=mock is the suite default; a real Groq call
only happens when GROQ_API_KEY is set and a developer deliberately asks for
it). The equivalent here is TEST_DATABASE_URL: unset, this entire module is
skipped and the 654 pre-existing tests are completely unaffected - no
Postgres install is required to run the suite. Set it to a scratch/test
database's DSN (e.g. postgresql://user:pass@localhost:5432/openhire_test)
to actually exercise repositories/postgres against a live server; CI can
set it once a Postgres service container is available.

These tests deliberately do not duplicate what tests/test_backend_*.py
already prove about the service layer - they exist to prove the Postgres
implementations honour the SAME contracts (interfaces.py's docstrings) that
repositories/memory.py's in-process stubs already satisfy: idempotent
upserts that preserve created_at, "never raises for absence", newest-first
ordering, the transcript-must-be-sealed rule, and
create_if_absent_for_session's atomicity.

Every test gets a fresh, empty set of the six tables (see the
`clean_tables` fixture) so tests never depend on each other's data or on
run order.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

# loop_scope="module": the `pool` fixture below is module-scoped and lazily
# opens ONE real asyncpg.Pool (PostgresConnectionPool.get(), on its first
# await) that every test in this module then shares. asyncpg binds a pool
# to whichever asyncio event loop was running when it was created.
# pytest-asyncio's default is a FRESH event loop per test function, so
# without this, the pool opened during test #1 would be bound to test #1's
# loop; by the time test #2 tried to acquire a connection from that same
# pool object, test #1's loop would already be closed - "RuntimeError:
# Event loop is closed". Pinning every test (and the async `clean_tables`
# fixture below, via its own loop_scope) to one shared per-module loop
# keeps the pool valid for the whole module, matching the pool's own
# module scope.
pytestmark = pytest.mark.asyncio(loop_scope="module")

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")

pytest.importorskip("asyncpg")

if not TEST_DATABASE_URL:
    pytest.skip(
        "TEST_DATABASE_URL is not set - skipping repositories/postgres "
        "contract tests (see this module's docstring). Set it to a "
        "scratch PostgreSQL database's DSN to run them.",
        allow_module_level=True,
    )

import asyncpg  # noqa: E402 - after the importorskip/skip above

from repositories.interfaces import (  # noqa: E402
    Application,
    CandidateRecord,
    EvaluationJob,
    EvaluationStatus,
    JobRecord,
    RepositoryError,
    SessionRecord,
)
from repositories.postgres import (  # noqa: E402
    PostgresApplicationRepository,
    PostgresCandidateRepository,
    PostgresConnectionPool,
    PostgresEvaluationRepository,
    PostgresJobRepository,
    PostgresSessionRepository,
    PostgresTranscriptRepository,
)
from schemas.application import ApplicationStatus  # noqa: E402
from schemas.evaluation import MatchingScore  # noqa: E402
from schemas.interview import InterviewAnswer, InterviewQuestion, InterviewTranscript  # noqa: E402
from schemas.job import Competency, JobDescription  # noqa: E402
from schemas.resume import ParsedResume  # noqa: E402
from schemas.scoring import CandidateReport, CandidateScores  # noqa: E402
from utils.interview_session import SessionStatus  # noqa: E402

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "repositories", "postgres", "schema.sql")

_TABLES = ("evaluations", "transcripts", "sessions", "applications", "candidates", "jobs")


@pytest.fixture(scope="module")
def pool():
    return PostgresConnectionPool(TEST_DATABASE_URL)


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean_tables(pool):
    """Apply the schema (idempotent - see schema.sql's own docstring) and
    truncate every table before each test, so tests never see another
    test's rows."""
    conn_pool = await pool.get()
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        schema_sql = fh.read()
    async with conn_pool.acquire() as conn:
        await conn.execute(schema_sql)
        await conn.execute(f"TRUNCATE {', '.join(_TABLES)} CASCADE")
    yield


def _job(job_id="job_pg_001"):
    return JobDescription(
        job_id=job_id,
        title="Backend Engineer",
        description="Test role",
        competencies=[Competency(name="Python", weight=0.5), Competency(name="SQL", weight=0.5)],
    )


def _resume(candidate_id="cand_pg_001"):
    return ParsedResume(candidate_id=candidate_id, candidate_name="Test Candidate", skills=["Python"])


async def _seed_job_and_candidate(pool, job_id="job_pg_001", candidate_id="cand_pg_001"):
    job_repo = PostgresJobRepository(pool)
    candidate_repo = PostgresCandidateRepository(pool)
    await job_repo.save(JobRecord(job_id=job_id, job=_job(job_id)))
    await candidate_repo.save(CandidateRecord(candidate_id=candidate_id, resume=_resume(candidate_id)))


class TestPostgresJobRepository:
    async def test_save_then_get_round_trips(self, pool):
        repo = PostgresJobRepository(pool)
        saved = await repo.save(JobRecord(job_id="job_a", job=_job("job_a")))
        assert saved.updated_at is not None

        fetched = await repo.get("job_a")
        assert fetched is not None
        assert fetched.job.title == "Backend Engineer"
        assert fetched.is_active is True

    async def test_get_missing_returns_none(self, pool):
        repo = PostgresJobRepository(pool)
        assert await repo.get("does-not-exist") is None

    async def test_save_twice_preserves_created_at(self, pool):
        repo = PostgresJobRepository(pool)
        first = await repo.save(JobRecord(job_id="job_b", job=_job("job_b")))
        second = await repo.save(JobRecord(job_id="job_b", job=_job("job_b")))
        assert first.created_at == second.created_at

    async def test_list_jobs_excludes_archived_by_default(self, pool):
        repo = PostgresJobRepository(pool)
        await repo.save(JobRecord(job_id="job_c", job=_job("job_c")))
        await repo.save(JobRecord(job_id="job_d", job=_job("job_d")))
        await repo.archive("job_c")

        active_only = await repo.list_jobs()
        assert {r.job_id for r in active_only} == {"job_d"}

        with_archived = await repo.list_jobs(include_archived=True)
        assert {r.job_id for r in with_archived} == {"job_c", "job_d"}

    async def test_archive_missing_job_returns_none(self, pool):
        repo = PostgresJobRepository(pool)
        assert await repo.archive("does-not-exist") is None


class TestPostgresCandidateRepository:
    async def test_save_then_get_round_trips(self, pool):
        repo = PostgresCandidateRepository(pool)
        await repo.save(
            CandidateRecord(candidate_id="cand_a", resume=_resume("cand_a"), used_fallback=True)
        )
        fetched = await repo.get("cand_a")
        assert fetched is not None
        assert fetched.used_fallback is True
        assert fetched.resume.candidate_name == "Test Candidate"

    async def test_get_many_ignores_missing_ids(self, pool):
        repo = PostgresCandidateRepository(pool)
        await repo.save(CandidateRecord(candidate_id="cand_b", resume=_resume("cand_b")))
        result = await repo.get_many(["cand_b", "does-not-exist"])
        assert {r.candidate_id for r in result} == {"cand_b"}


class TestPostgresApplicationRepository:
    async def test_duplicate_job_candidate_pair_is_rejected(self, pool):
        await _seed_job_and_candidate(pool)
        repo = PostgresApplicationRepository(pool)
        await repo.save(
            Application(application_id="app_1", job_id="job_pg_001", candidate_id="cand_pg_001")
        )
        with pytest.raises(RepositoryError):
            await repo.save(
                Application(application_id="app_2", job_id="job_pg_001", candidate_id="cand_pg_001")
            )

    async def test_get_for_job_and_candidate(self, pool):
        await _seed_job_and_candidate(pool)
        repo = PostgresApplicationRepository(pool)
        await repo.save(
            Application(application_id="app_3", job_id="job_pg_001", candidate_id="cand_pg_001")
        )
        found = await repo.get_for_job_and_candidate("job_pg_001", "cand_pg_001")
        assert found is not None
        assert found.application_id == "app_3"
        assert await repo.get_for_job_and_candidate("job_pg_001", "no-such-candidate") is None

    async def test_matching_score_round_trips(self, pool):
        await _seed_job_and_candidate(pool)
        repo = PostgresApplicationRepository(pool)
        score = MatchingScore(
            match_id="match_1",
            candidate_id="cand_pg_001",
            job_id="job_pg_001",
            match_score=0.8,
            experience_match=0.8,
            skill_gap=0.2,
            explanation="good fit",
            confidence=0.9,
        )
        await repo.save(
            Application(
                application_id="app_4",
                job_id="job_pg_001",
                candidate_id="cand_pg_001",
                status=ApplicationStatus.SHORTLISTED,
                matching_score=score,
            )
        )
        fetched = await repo.get("app_4")
        assert fetched.status == ApplicationStatus.SHORTLISTED
        assert fetched.matching_score.match_score == 0.8

    async def test_list_for_job_and_candidate(self, pool):
        await _seed_job_and_candidate(pool, job_id="job_x", candidate_id="cand_x")
        await _seed_job_and_candidate(pool, job_id="job_y", candidate_id="cand_x")
        repo = PostgresApplicationRepository(pool)
        await repo.save(Application(application_id="app_5", job_id="job_x", candidate_id="cand_x"))
        await repo.save(Application(application_id="app_6", job_id="job_y", candidate_id="cand_x"))

        assert {a.application_id for a in await repo.list_for_candidate("cand_x")} == {
            "app_5",
            "app_6",
        }
        assert {a.application_id for a in await repo.list_for_job("job_x")} == {"app_5"}


class TestPostgresSessionRepository:
    async def test_snapshot_round_trips_and_is_not_a_live_reference(self, pool):
        """The point of the snapshot design (audit section 9): the session
        keeps ITS OWN copy of the job/resume, independent of the jobs/
        candidates tables."""
        await _seed_job_and_candidate(pool, job_id="job_snap", candidate_id="cand_snap")
        repo = PostgresSessionRepository(pool)
        job_snapshot = _job("job_snap")
        resume_snapshot = _resume("cand_snap")

        await repo.save(
            SessionRecord(
                session_id="sess_1",
                candidate_id="cand_snap",
                job_id="job_snap",
                status=SessionStatus.CREATED,
                interview_id="int_1",
                job_description=job_snapshot,
                parsed_resume=resume_snapshot,
            )
        )

        # Now mutate the LIVE job via the job repository - the session's
        # own snapshot must be unaffected.
        job_repo = PostgresJobRepository(pool)
        live = await job_repo.get("job_snap")
        edited = live.job.model_copy(update={"title": "Changed Title"})
        await job_repo.save(JobRecord(job_id="job_snap", job=edited))

        fetched = await repo.get("sess_1")
        assert fetched.job_description.title == "Backend Engineer"

    async def test_save_twice_preserves_created_at(self, pool):
        await _seed_job_and_candidate(pool, job_id="job_snap2", candidate_id="cand_snap2")
        repo = PostgresSessionRepository(pool)
        first = await repo.save(
            SessionRecord(
                session_id="sess_2",
                candidate_id="cand_snap2",
                job_id="job_snap2",
                status=SessionStatus.CREATED,
            )
        )
        second = await repo.save(
            SessionRecord(
                session_id="sess_2",
                candidate_id="cand_snap2",
                job_id="job_snap2",
                status=SessionStatus.ACTIVE,
            )
        )
        assert first.created_at == second.created_at
        assert second.status == SessionStatus.ACTIVE

    async def test_list_for_candidate_newest_first(self, pool):
        await _seed_job_and_candidate(pool, job_id="job_snap3", candidate_id="cand_snap3")
        repo = PostgresSessionRepository(pool)
        await repo.save(
            SessionRecord(session_id="sess_3", candidate_id="cand_snap3", job_id="job_snap3",
                          status=SessionStatus.CREATED)
        )
        await repo.save(
            SessionRecord(session_id="sess_4", candidate_id="cand_snap3", job_id="job_snap3",
                          status=SessionStatus.CREATED)
        )
        results = await repo.list_for_candidate("cand_snap3")
        assert [r.created_at for r in results] == sorted(
            (r.created_at for r in results), reverse=True
        )

    async def test_get_many_and_delete(self, pool):
        await _seed_job_and_candidate(pool, job_id="job_snap4", candidate_id="cand_snap4")
        repo = PostgresSessionRepository(pool)
        await repo.save(
            SessionRecord(session_id="sess_5", candidate_id="cand_snap4", job_id="job_snap4",
                          status=SessionStatus.CREATED)
        )
        found = await repo.get_many(["sess_5", "does-not-exist"])
        assert {r.session_id for r in found} == {"sess_5"}

        assert await repo.delete("sess_5") is True
        assert await repo.delete("sess_5") is False
        assert await repo.get("sess_5") is None


class TestPostgresTranscriptRepository:
    def _sealed_transcript(self, interview_id="int_seal_1"):
        exchanges = [
            (
                InterviewQuestion(question_id="q1", question_text="Tell me about yourself.", category="technical"),
                InterviewAnswer(question_id="q1", answer_text="I write Python."),
            )
        ]
        return InterviewTranscript(
            interview_id=interview_id,
            candidate_id="cand_pg_001",
            job_id="job_pg_001",
            start_time="2024-01-01T00:00:00Z",
            exchanges=exchanges,
            is_sealed=True,
        )

    async def test_rejects_unsealed_transcript(self, pool):
        repo = PostgresTranscriptRepository(pool)
        unsealed = self._sealed_transcript().model_copy(update={"is_sealed": False})
        with pytest.raises(RepositoryError):
            await repo.save(unsealed)

    async def test_save_then_get_round_trips_exchanges(self, pool):
        await _seed_job_and_candidate(pool)
        await PostgresSessionRepository(pool).save(
            SessionRecord(session_id="sess_seal", candidate_id="cand_pg_001", job_id="job_pg_001",
                          status=SessionStatus.SEALED, interview_id="int_seal_1")
        )
        repo = PostgresTranscriptRepository(pool)
        await repo.save(self._sealed_transcript())

        fetched = await repo.get("int_seal_1")
        assert fetched is not None
        assert len(fetched.exchanges) == 1
        question, answer = fetched.exchanges[0]
        assert question.question_text == "Tell me about yourself."
        assert answer.answer_text == "I write Python."

    async def test_get_for_candidate_scoped_by_job(self, pool):
        await _seed_job_and_candidate(pool)
        await PostgresSessionRepository(pool).save(
            SessionRecord(session_id="sess_seal2", candidate_id="cand_pg_001", job_id="job_pg_001",
                          status=SessionStatus.SEALED, interview_id="int_seal_2")
        )
        repo = PostgresTranscriptRepository(pool)
        await repo.save(self._sealed_transcript("int_seal_2"))

        found = await repo.get_for_candidate("cand_pg_001", "job_pg_001")
        assert {t.interview_id for t in found} == {"int_seal_2"}
        assert await repo.get_for_candidate("cand_pg_001", "no-such-job") == []


class TestPostgresEvaluationRepository:
    async def _seeded_session(self, pool, session_id="sess_eval_1", interview_id="int_eval_1"):
        await _seed_job_and_candidate(pool)
        await PostgresSessionRepository(pool).save(
            SessionRecord(session_id=session_id, candidate_id="cand_pg_001", job_id="job_pg_001",
                          status=SessionStatus.SEALED, interview_id=interview_id)
        )

    async def test_create_if_absent_for_session_is_idempotent(self, pool):
        await self._seeded_session(pool)
        repo = PostgresEvaluationRepository(pool)
        first = await repo.create_if_absent_for_session(
            EvaluationJob(
                evaluation_id="eval_1", session_id="sess_eval_1", interview_id="int_eval_1",
                candidate_id="cand_pg_001", job_id="job_pg_001",
            )
        )
        second = await repo.create_if_absent_for_session(
            EvaluationJob(
                evaluation_id="eval_2", session_id="sess_eval_1", interview_id="int_eval_1",
                candidate_id="cand_pg_001", job_id="job_pg_001",
            )
        )
        # The second call's job must be discarded - "existing wins".
        assert first.evaluation_id == "eval_1"
        assert second.evaluation_id == "eval_1"

    async def test_result_round_trips_and_completed_requires_result(self, pool):
        await self._seeded_session(pool, "sess_eval_2", "int_eval_2")
        repo = PostgresEvaluationRepository(pool)
        report = CandidateReport(
            report_id="report_1",
            candidate_id="cand_pg_001",
            job_id="job_pg_001",
            candidate_name="Test Candidate",
            scores=CandidateScores(
                score_id="scores_1", candidate_id="cand_pg_001", job_id="job_pg_001",
                technical_score=8.0, behavioral_score=7.0, job_fit_score=7.5,
                weighted_final_score=7.5, explanation="solid", confidence=0.9,
            ),
            technical_summary="good", behavioral_summary="good", job_fit_summary="good",
            recommendation="candidate", explanation="overall good",
        )
        job = EvaluationJob(
            evaluation_id="eval_3", session_id="sess_eval_2", interview_id="int_eval_2",
            candidate_id="cand_pg_001", job_id="job_pg_001",
            status=EvaluationStatus.COMPLETED, result=report,
        )
        saved = await repo.save(job)
        assert saved.result.scores.weighted_final_score == 7.5

        fetched = await repo.get("eval_3")
        assert fetched.status == EvaluationStatus.COMPLETED
        assert fetched.result.candidate_name == "Test Candidate"

    async def test_completed_without_result_is_rejected(self, pool):
        await self._seeded_session(pool, "sess_eval_3", "int_eval_3")
        repo = PostgresEvaluationRepository(pool)
        job = EvaluationJob(
            evaluation_id="eval_4", session_id="sess_eval_3", interview_id="int_eval_3",
            candidate_id="cand_pg_001", job_id="job_pg_001",
            status=EvaluationStatus.COMPLETED, result=None,
        )
        with pytest.raises(asyncpg.PostgresError):
            await repo.save(job)

    async def test_get_many_for_sessions_keyed_by_session_id(self, pool):
        await self._seeded_session(pool, "sess_eval_4", "int_eval_4")
        repo = PostgresEvaluationRepository(pool)
        await repo.create_if_absent_for_session(
            EvaluationJob(
                evaluation_id="eval_5", session_id="sess_eval_4", interview_id="int_eval_4",
                candidate_id="cand_pg_001", job_id="job_pg_001",
            )
        )
        result = await repo.get_many_for_sessions(["sess_eval_4", "no-such-session"])
        assert set(result.keys()) == {"sess_eval_4"}
        assert result["sess_eval_4"].evaluation_id == "eval_5"
