"""
PostgreSQL implementations of OpenHire data-access repositories.
Satisfies the contracts defined in repositories/interfaces.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore

from core.logging import get_logger
from repositories.interfaces import (
    Application,
    ApplicationRepository,
    CandidateRecord,
    CandidateRepository,
    EvaluationJob,
    EvaluationRepository,
    EvaluationStatus,
    JobRecord,
    JobRepository,
    RepositoryError,
    SessionRecord,
    SessionRepository,
    TranscriptRepository,
)
from schemas.interview import InterviewTranscript
from schemas.job import JobDescription
from schemas.resume import ParsedResume
from schemas.scoring import CandidateReport

logger = get_logger("repositories.postgres")


def _dumps(obj: Any) -> Optional[str]:
    """Helper to dump pydantic / dict to json string."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return json.dumps(obj.model_dump(mode="json"))
    if isinstance(obj, (dict, list)):
        return json.dumps(obj)
    return json.dumps(obj)


def _loads(data: Any) -> Any:
    """Helper to parse JSON string if needed."""
    if data is None:
        return None
    if isinstance(data, str):
        return json.loads(data)
    return data


# ============================================================================
# 1. PostgresSessionRepository
# ============================================================================
class PostgresSessionRepository(SessionRepository):
    def __init__(self, pool: "asyncpg.Pool"):
        self.pool = pool

    async def save(self, record: SessionRecord) -> SessionRecord:
        now = datetime.now(timezone.utc)
        record_to_save = record.model_copy(update={"updated_at": now})

        query = """
            INSERT INTO interviews (
                id, session_id, job_id, candidate_id, application_id,
                link_token, status, termination_reason, questions_asked,
                questions_answered, state, job_description, parsed_resume,
                created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            ON CONFLICT (session_id) DO UPDATE SET
                status = EXCLUDED.status,
                termination_reason = EXCLUDED.termination_reason,
                questions_asked = EXCLUDED.questions_asked,
                questions_answered = EXCLUDED.questions_answered,
                state = EXCLUDED.state,
                job_description = EXCLUDED.job_description,
                parsed_resume = EXCLUDED.parsed_resume,
                updated_at = EXCLUDED.updated_at
            RETURNING created_at, updated_at;
        """
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    query,
                    record.interview_id or record.session_id,
                    record.session_id,
                    record.job_id,
                    record.candidate_id,
                    record.application_id,
                    record.session_id,  # default link_token
                    record.status.value if hasattr(record.status, "value") else str(record.status),
                    record.termination_reason,
                    record.questions_asked,
                    record.questions_answered,
                    _dumps(record.state),
                    _dumps(record.job_description),
                    _dumps(record.parsed_resume),
                    record.created_at,
                    now,
                )
                if row:
                    return record_to_save.model_copy(update={
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    })
                return record_to_save
        except Exception as exc:
            raise RepositoryError(f"Failed to save session: {exc}") from exc

    def _row_to_record(self, row: dict) -> SessionRecord:
        state_data = _loads(row["state"])
        job_data = _loads(row["job_description"])
        resume_data = _loads(row["parsed_resume"])

        from schemas.interview import InterviewState
        state = InterviewState.model_validate(state_data) if state_data else None
        job_desc = JobDescription.model_validate(job_data) if job_data else None
        parsed_resume = ParsedResume.model_validate(resume_data) if resume_data else None

        from utils.interview_session import SessionStatus
        return SessionRecord(
            session_id=row["session_id"],
            candidate_id=row["candidate_id"],
            job_id=row["job_id"],
            interview_id=row["id"],
            application_id=row["application_id"],
            status=SessionStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            termination_reason=row["termination_reason"],
            questions_asked=row["questions_asked"],
            questions_answered=row["questions_answered"],
            state=state,
            job_description=job_desc,
            parsed_resume=parsed_resume,
        )

    async def get(self, session_id: str) -> Optional[SessionRecord]:
        query = "SELECT * FROM interviews WHERE session_id = $1;"
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(query, session_id)
                if not row:
                    return None
                return self._row_to_record(dict(row))
        except Exception as exc:
            raise RepositoryError(f"Failed to get session: {exc}") from exc

    async def list_for_candidate(self, candidate_id: str) -> list[SessionRecord]:
        query = "SELECT * FROM interviews WHERE candidate_id = $1 ORDER BY created_at DESC;"
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, candidate_id)
                return [self._row_to_record(dict(r)) for r in rows]
        except Exception as exc:
            raise RepositoryError(f"Failed to list sessions: {exc}") from exc

    async def delete(self, session_id: str) -> bool:
        query = "DELETE FROM interviews WHERE session_id = $1;"
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(query, session_id)
                return result == "DELETE 1"
        except Exception as exc:
            raise RepositoryError(f"Failed to delete session: {exc}") from exc

    async def get_many(self, session_ids: list[str]) -> list[SessionRecord]:
        if not session_ids:
            return []
        query = "SELECT * FROM interviews WHERE session_id = ANY($1::varchar[]);"
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, session_ids)
                return [self._row_to_record(dict(r)) for r in rows]
        except Exception as exc:
            raise RepositoryError(f"Failed to get_many sessions: {exc}") from exc


# ============================================================================
# 2. PostgresTranscriptRepository
# ============================================================================
class PostgresTranscriptRepository(TranscriptRepository):
    def __init__(self, pool: "asyncpg.Pool"):
        self.pool = pool

    async def save(self, transcript: InterviewTranscript) -> None:
        if not transcript.is_sealed:
            raise RepositoryError("Cannot persist unsealed transcript")

        query = """
            INSERT INTO transcripts (
                id, interview_id, candidate_id, job_id, question_index,
                answer_text, audio_url, transcript_data, is_sealed, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (id) DO UPDATE SET
                transcript_data = EXCLUDED.transcript_data,
                is_sealed = EXCLUDED.is_sealed;
        """
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    query,
                    transcript.interview_id,
                    transcript.interview_id,
                    transcript.candidate_id,
                    transcript.job_id,
                    len(transcript.exchanges),
                    transcript.summary or "",
                    None,
                    _dumps(transcript),
                    transcript.is_sealed,
                    transcript.created_at,
                )
        except Exception as exc:
            raise RepositoryError(f"Failed to save transcript: {exc}") from exc

    async def get(self, interview_id: str) -> Optional[InterviewTranscript]:
        query = "SELECT transcript_data FROM transcripts WHERE interview_id = $1;"
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(query, interview_id)
                if not row:
                    return None
                data = _loads(row["transcript_data"])
                return InterviewTranscript.model_validate(data)
        except Exception as exc:
            raise RepositoryError(f"Failed to get transcript: {exc}") from exc

    async def get_for_candidate(self, candidate_id: str, job_id: str) -> list[InterviewTranscript]:
        query = "SELECT transcript_data FROM transcripts WHERE candidate_id = $1 AND job_id = $2;"
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, candidate_id, job_id)
                return [InterviewTranscript.model_validate(_loads(r["transcript_data"])) for r in rows]
        except Exception as exc:
            raise RepositoryError(f"Failed to get transcripts for candidate: {exc}") from exc


# ============================================================================
# 3. PostgresJobRepository
# ============================================================================
class PostgresJobRepository(JobRepository):
    def __init__(self, pool: "asyncpg.Pool"):
        self.pool = pool

    async def save(self, record: JobRecord) -> JobRecord:
        now = datetime.now(timezone.utc)
        record_to_save = record.model_copy(update={"updated_at": now})

        questions_json = _dumps([q.model_dump() if hasattr(q, "model_dump") else q for q in record.job.questions]) if record.job.questions else "[]"
        query = """
            INSERT INTO jobs (id, title, questions, is_active, job_data, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                questions = EXCLUDED.questions,
                is_active = EXCLUDED.is_active,
                job_data = EXCLUDED.job_data,
                updated_at = EXCLUDED.updated_at
            RETURNING created_at, updated_at;
        """
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    query,
                    record.job_id,
                    record.job.title,
                    questions_json,
                    record.is_active,
                    _dumps(record.job),
                    record.created_at,
                    now,
                )
                if row:
                    record_to_save.created_at = row["created_at"]
                    record_to_save.updated_at = row["updated_at"]
                return record_to_save
        except Exception as exc:
            raise RepositoryError(f"Failed to save job: {exc}") from exc

    def _row_to_record(self, row: dict) -> JobRecord:
        job_data = _loads(row["job_data"])
        job = JobDescription.model_validate(job_data)
        return JobRecord(
            job_id=row["id"],
            job=job,
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get(self, job_id: str) -> Optional[JobRecord]:
        query = "SELECT * FROM jobs WHERE id = $1;"
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(query, job_id)
                if not row:
                    return None
                return self._row_to_record(dict(row))
        except Exception as exc:
            raise RepositoryError(f"Failed to get job: {exc}") from exc

    async def list_jobs(self, *, include_archived: bool = False) -> list[JobRecord]:
        query = "SELECT * FROM jobs" + ("" if include_archived else " WHERE is_active = TRUE") + " ORDER BY created_at DESC;"
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query)
                return [self._row_to_record(dict(r)) for r in rows]
        except Exception as exc:
            raise RepositoryError(f"Failed to list jobs: {exc}") from exc

    async def archive(self, job_id: str) -> Optional[JobRecord]:
        query = "UPDATE jobs SET is_active = FALSE, updated_at = $2 WHERE id = $1 RETURNING *;"
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(query, job_id, datetime.now(timezone.utc))
                if not row:
                    return None
                return self._row_to_record(dict(row))
        except Exception as exc:
            raise RepositoryError(f"Failed to archive job: {exc}") from exc


# ============================================================================
# 4. PostgresCandidateRepository
# ============================================================================
class PostgresCandidateRepository(CandidateRepository):
    def __init__(self, pool: "asyncpg.Pool"):
        self.pool = pool

    async def save(self, record: CandidateRecord) -> CandidateRecord:
        now = datetime.now(timezone.utc)
        record_to_save = record.model_copy(update={"updated_at": now})

        query = """
            INSERT INTO candidates (
                id, name, email, resume, used_fallback, parse_warning, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                email = EXCLUDED.email,
                resume = EXCLUDED.resume,
                used_fallback = EXCLUDED.used_fallback,
                parse_warning = EXCLUDED.parse_warning,
                updated_at = EXCLUDED.updated_at
            RETURNING created_at, updated_at;
        """
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    query,
                    record.candidate_id,
                    record.resume.name if record.resume else None,
                    record.resume.email if record.resume else None,
                    _dumps(record.resume),
                    record.used_fallback,
                    record.parse_warning,
                    record.created_at,
                    now,
                )
                if row:
                    record_to_save.created_at = row["created_at"]
                    record_to_save.updated_at = row["updated_at"]
                return record_to_save
        except Exception as exc:
            raise RepositoryError(f"Failed to save candidate: {exc}") from exc

    def _row_to_record(self, row: dict) -> CandidateRecord:
        resume_data = _loads(row["resume"])
        resume = ParsedResume.model_validate(resume_data)
        return CandidateRecord(
            candidate_id=row["id"],
            resume=resume,
            used_fallback=row["used_fallback"],
            parse_warning=row["parse_warning"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get(self, candidate_id: str) -> Optional[CandidateRecord]:
        query = "SELECT * FROM candidates WHERE id = $1;"
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(query, candidate_id)
                if not row:
                    return None
                return self._row_to_record(dict(row))
        except Exception as exc:
            raise RepositoryError(f"Failed to get candidate: {exc}") from exc

    async def list_candidates(self) -> list[CandidateRecord]:
        query = "SELECT * FROM candidates ORDER BY created_at DESC;"
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query)
                return [self._row_to_record(dict(r)) for r in rows]
        except Exception as exc:
            raise RepositoryError(f"Failed to list candidates: {exc}") from exc

    async def get_many(self, candidate_ids: list[str]) -> list[CandidateRecord]:
        if not candidate_ids:
            return []
        query = "SELECT * FROM candidates WHERE id = ANY($1::varchar[]);"
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, candidate_ids)
                return [self._row_to_record(dict(r)) for r in rows]
        except Exception as exc:
            raise RepositoryError(f"Failed to get_many candidates: {exc}") from exc


# ============================================================================
# 5. PostgresApplicationRepository
# ============================================================================
class PostgresApplicationRepository(ApplicationRepository):
    def __init__(self, pool: "asyncpg.Pool"):
        self.pool = pool

    async def save(self, application: Application) -> Application:
        now = datetime.now(timezone.utc)
        query = """
            INSERT INTO applications (
                id, job_id, candidate_id, status, matching_score,
                session_id, evaluation_id, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                matching_score = EXCLUDED.matching_score,
                session_id = EXCLUDED.session_id,
                evaluation_id = EXCLUDED.evaluation_id,
                updated_at = EXCLUDED.updated_at
            RETURNING created_at, updated_at;
        """
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    query,
                    application.application_id,
                    application.job_id,
                    application.candidate_id,
                    application.status.value if hasattr(application.status, "value") else str(application.status),
                    _dumps(application.matching_score),
                    application.session_id,
                    application.evaluation_id,
                    application.created_at,
                    now,
                )
                return application.model_copy(update={
                    "created_at": row["created_at"] if row else application.created_at,
                    "updated_at": row["updated_at"] if row else now,
                })
        except Exception as exc:
            raise RepositoryError(f"Failed to save application: {exc}") from exc

    def _row_to_app(self, row: dict) -> Application:
        match_data = _loads(row["matching_score"])
        from schemas.application import ApplicationStatus
        from schemas.matching import MatchingScore
        return Application(
            application_id=row["id"],
            job_id=row["job_id"],
            candidate_id=row["candidate_id"],
            status=ApplicationStatus(row["status"]),
            matching_score=MatchingScore.model_validate(match_data) if match_data else None,
            session_id=row["session_id"],
            evaluation_id=row["evaluation_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get(self, application_id: str) -> Optional[Application]:
        query = "SELECT * FROM applications WHERE id = $1;"
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(query, application_id)
                if not row:
                    return None
                return self._row_to_app(dict(row))
        except Exception as exc:
            raise RepositoryError(f"Failed to get application: {exc}") from exc

    async def get_for_job_and_candidate(self, job_id: str, candidate_id: str) -> Optional[Application]:
        query = "SELECT * FROM applications WHERE job_id = $1 AND candidate_id = $2;"
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(query, job_id, candidate_id)
                if not row:
                    return None
                return self._row_to_app(dict(row))
        except Exception as exc:
            raise RepositoryError(f"Failed to get application for job and candidate: {exc}") from exc

    async def list_for_job(self, job_id: str) -> list[Application]:
        query = "SELECT * FROM applications WHERE job_id = $1 ORDER BY created_at DESC;"
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, job_id)
                return [self._row_to_app(dict(r)) for r in rows]
        except Exception as exc:
            raise RepositoryError(f"Failed to list applications for job: {exc}") from exc

    async def list_for_candidate(self, candidate_id: str) -> list[Application]:
        query = "SELECT * FROM applications WHERE candidate_id = $1 ORDER BY created_at DESC;"
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, candidate_id)
                return [self._row_to_app(dict(r)) for r in rows]
        except Exception as exc:
            raise RepositoryError(f"Failed to list applications for candidate: {exc}") from exc


# ============================================================================
# 6. PostgresEvaluationRepository
# ============================================================================
class PostgresEvaluationRepository(EvaluationRepository):
    def __init__(self, pool: "asyncpg.Pool"):
        self.pool = pool

    async def save(self, job: EvaluationJob) -> EvaluationJob:
        now = datetime.now(timezone.utc)
        job_to_save = job.model_copy(update={"updated_at": now})

        score = job.result.overall_score if job.result else None
        report_text = job.result.executive_summary if job.result else None

        query = """
            INSERT INTO evaluations (
                id, session_id, interview_id, candidate_id, job_id,
                application_id, status, overall_score, report_text,
                result, error, warnings, created_at, updated_at, started_at, completed_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                overall_score = EXCLUDED.overall_score,
                report_text = EXCLUDED.report_text,
                result = EXCLUDED.result,
                error = EXCLUDED.error,
                warnings = EXCLUDED.warnings,
                updated_at = EXCLUDED.updated_at,
                started_at = EXCLUDED.started_at,
                completed_at = EXCLUDED.completed_at
            RETURNING created_at, updated_at;
        """
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    query,
                    job.evaluation_id,
                    job.session_id,
                    job.interview_id,
                    job.candidate_id,
                    job.job_id,
                    job.application_id,
                    job.status.value if hasattr(job.status, "value") else str(job.status),
                    score,
                    report_text,
                    _dumps(job.result),
                    job.error,
                    _dumps(job.warnings or []),
                    job.created_at,
                    now,
                    job.started_at,
                    job.completed_at,
                )
                if row:
                    job_to_save.created_at = row["created_at"]
                    job_to_save.updated_at = row["updated_at"]
                return job_to_save
        except Exception as exc:
            raise RepositoryError(f"Failed to save evaluation job: {exc}") from exc

    def _row_to_job(self, row: dict) -> EvaluationJob:
        res_data = _loads(row["result"])
        warn_data = _loads(row["warnings"]) or []
        return EvaluationJob(
            evaluation_id=row["id"],
            session_id=row["session_id"],
            interview_id=row["interview_id"],
            candidate_id=row["candidate_id"],
            job_id=row["job_id"],
            application_id=row["application_id"],
            status=EvaluationStatus(row["status"]),
            result=CandidateReport.model_validate(res_data) if res_data else None,
            error=row["error"],
            warnings=warn_data if isinstance(warn_data, list) else [],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    async def get(self, evaluation_id: str) -> Optional[EvaluationJob]:
        query = "SELECT * FROM evaluations WHERE id = $1;"
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(query, evaluation_id)
                if not row:
                    return None
                return self._row_to_job(dict(row))
        except Exception as exc:
            raise RepositoryError(f"Failed to get evaluation: {exc}") from exc

    async def get_for_session(self, session_id: str) -> Optional[EvaluationJob]:
        query = "SELECT * FROM evaluations WHERE session_id = $1;"
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(query, session_id)
                if not row:
                    return None
                return self._row_to_job(dict(row))
        except Exception as exc:
            raise RepositoryError(f"Failed to get evaluation for session: {exc}") from exc

    async def create_if_absent_for_session(self, job: EvaluationJob) -> EvaluationJob:
        query = """
            INSERT INTO evaluations (
                id, session_id, interview_id, candidate_id, job_id,
                application_id, status, error, warnings, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (session_id) DO NOTHING
            RETURNING *;
        """
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    query,
                    job.evaluation_id,
                    job.session_id,
                    job.interview_id,
                    job.candidate_id,
                    job.job_id,
                    job.application_id,
                    job.status.value if hasattr(job.status, "value") else str(job.status),
                    job.error,
                    _dumps(job.warnings or []),
                    job.created_at,
                    datetime.now(timezone.utc),
                )
                if row:
                    return self._row_to_job(dict(row))
                existing = await self.get_for_session(job.session_id)
                if existing:
                    return existing
                raise RepositoryError("create_if_absent_for_session conflict resolution failed")
        except Exception as exc:
            raise RepositoryError(f"Failed create_if_absent_for_session: {exc}") from exc

    async def get_many_for_sessions(self, session_ids: list[str]) -> dict[str, EvaluationJob]:
        if not session_ids:
            return {}
        query = "SELECT * FROM evaluations WHERE session_id = ANY($1::varchar[]);"
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, session_ids)
                return {r["session_id"]: self._row_to_job(dict(r)) for r in rows}
        except Exception as exc:
            raise RepositoryError(f"Failed get_many_for_sessions: {exc}") from exc


# ============================================================================
# 7. Stage 2: PostgresRubricRepository
# ============================================================================
class PostgresRubricRepository:
    def __init__(self, pool: "asyncpg.Pool"):
        self.pool = pool

    async def save_rubric(
        self,
        rubric_id: str,
        job_id: str,
        interview_type: str,
        competencies: list[dict],
        pass_threshold: float = 6.0,
    ) -> dict:
        now = datetime.now(timezone.utc)
        query = """
            INSERT INTO job_rubrics (id, job_id, interview_type, competencies, pass_threshold, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (job_id, interview_type) DO UPDATE SET
                competencies = EXCLUDED.competencies,
                pass_threshold = EXCLUDED.pass_threshold,
                updated_at = EXCLUDED.updated_at
            RETURNING *;
        """
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    query,
                    rubric_id,
                    job_id,
                    interview_type,
                    _dumps(competencies),
                    pass_threshold,
                    now,
                    now,
                )
                return dict(row) if row else {}
        except Exception as exc:
            raise RepositoryError(f"Failed to save rubric: {exc}") from exc

    async def get_rubric(self, job_id: str, interview_type: str = "technical") -> Optional[dict]:
        query = "SELECT * FROM job_rubrics WHERE job_id = $1 AND interview_type = $2;"
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(query, job_id, interview_type)
                return dict(row) if row else None
        except Exception as exc:
            raise RepositoryError(f"Failed to get rubric: {exc}") from exc


# ============================================================================
# 8. Stage 2: PostgresLeaderboardRepository
# ============================================================================
class PostgresLeaderboardRepository:
    def __init__(self, pool: "asyncpg.Pool"):
        self.pool = pool

    async def save_leaderboard(self, job_id: str, ranked_entries: list[dict], summary: dict) -> None:
        query = """
            INSERT INTO job_leaderboards (id, job_id, ranked_entries, summary, updated_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (job_id) DO UPDATE SET
                ranked_entries = EXCLUDED.ranked_entries,
                summary = EXCLUDED.summary,
                updated_at = EXCLUDED.updated_at;
        """
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    query,
                    f"lead_{job_id}",
                    job_id,
                    _dumps(ranked_entries),
                    _dumps(summary),
                    datetime.now(timezone.utc),
                )
        except Exception as exc:
            raise RepositoryError(f"Failed to save leaderboard: {exc}") from exc

    async def get_leaderboard(self, job_id: str) -> Optional[dict]:
        query = "SELECT * FROM job_leaderboards WHERE job_id = $1;"
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(query, job_id)
                return dict(row) if row else None
        except Exception as exc:
            raise RepositoryError(f"Failed to get leaderboard: {exc}") from exc


# ============================================================================
# 9. Stage 3: PostgresAuditRepository
# ============================================================================
class PostgresAuditRepository:
    def __init__(self, pool: "asyncpg.Pool"):
        self.pool = pool

    async def save_pipeline_run(self, run: Any) -> None:
        query = """
            INSERT INTO pipeline_runs (
                run_id, job_id, stage, status, candidate_ids, start_time,
                end_time, duration_seconds, total_agents_executed,
                successful_agents, failed_agents, errors, warnings
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            ON CONFLICT (run_id) DO UPDATE SET
                stage = EXCLUDED.stage,
                status = EXCLUDED.status,
                end_time = EXCLUDED.end_time,
                duration_seconds = EXCLUDED.duration_seconds,
                total_agents_executed = EXCLUDED.total_agents_executed,
                successful_agents = EXCLUDED.successful_agents,
                failed_agents = EXCLUDED.failed_agents,
                errors = EXCLUDED.errors,
                warnings = EXCLUDED.warnings;
        """
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    query,
                    run.run_id,
                    run.job_id,
                    run.stage,
                    run.status,
                    _dumps(run.candidate_ids),
                    datetime.fromisoformat(run.start_time) if isinstance(run.start_time, str) else run.start_time,
                    datetime.fromisoformat(run.end_time) if isinstance(run.end_time, str) and run.end_time else None,
                    run.duration_seconds,
                    run.total_agents_executed,
                    run.successful_agents,
                    run.failed_agents,
                    _dumps(run.errors),
                    _dumps(run.warnings),
                )
        except Exception as exc:
            raise RepositoryError(f"Failed to save pipeline run: {exc}") from exc

    async def save_audit_log(self, log: Any) -> None:
        query = """
            INSERT INTO agent_audit_logs (
                log_id, run_id, agent_name, status, input_keys, output_keys,
                evidence_ids, model_name, provider, attempt_number,
                max_attempts, error_message, start_time, end_time, duration_seconds
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            ON CONFLICT (log_id) DO UPDATE SET
                status = EXCLUDED.status,
                output_keys = EXCLUDED.output_keys,
                evidence_ids = EXCLUDED.evidence_ids,
                error_message = EXCLUDED.error_message,
                end_time = EXCLUDED.end_time,
                duration_seconds = EXCLUDED.duration_seconds;
        """
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    query,
                    log.log_id,
                    log.run_id,
                    log.agent_name,
                    log.status,
                    _dumps(log.input_keys),
                    _dumps(log.output_keys),
                    _dumps(log.evidence_ids),
                    log.model_name,
                    log.provider,
                    log.attempt_number,
                    log.max_attempts,
                    log.error_message,
                    datetime.fromisoformat(log.start_time) if isinstance(log.start_time, str) else log.start_time,
                    datetime.fromisoformat(log.end_time) if isinstance(log.end_time, str) and log.end_time else None,
                    log.duration_seconds,
                )
        except Exception as exc:
            raise RepositoryError(f"Failed to save audit log: {exc}") from exc
