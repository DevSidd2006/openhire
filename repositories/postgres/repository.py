"""
PostgreSQL implementations of the six repository ABCs in
repositories/interfaces.py, backed by repositories/postgres/schema.sql.

Every method below implements the EXACT contract documented on its
abstract base in repositories/interfaces.py - idempotent upserts that
preserve `created_at`, "never raises for absence" reads, the same
"newest first" ordering the in-memory stubs (repositories/memory.py) use,
and the one hard atomicity requirement
(`EvaluationRepository.create_if_absent_for_session`). Nothing here changes
what a caller can observe compared to the in-memory stubs; it only changes
where the data lives.

JSONB fields are read/written as plain Python dict/list values, not JSON
strings - see repositories/postgres/pool.py's connection-level codec. Every
domain object embedded in a JSONB column round-trips through
`model_dump(mode="json")` on the way in and `Model.model_validate(...)` on
the way out, so a Pydantic field rename is the only thing that can break
this mapping - there is no separate, hand-maintained column-by-column
serialization to keep in sync.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import asyncpg

from schemas.rubric import JobRubric, RubricStatus

from repositories.interfaces import (
    RubricRepository,
    Application,
    ApplicationRepository,
    BugReportRecord,
    BugReportRepository,
    BugSeverity,
    BugStatus,
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
from repositories.postgres.pool import PostgresConnectionPool
from schemas.application import ApplicationStatus
from schemas.evaluation import MatchingScore
from schemas.interview import (
    InterviewAnswer,
    InterviewQuestion,
    InterviewState,
    InterviewTranscript,
)
from schemas.job import JobDescription
from schemas.resume import ParsedResume
from schemas.scoring import CandidateReport
from utils.interview_session import SessionStatus


# ---------------------------------------------------------------------
# Row <-> model mapping helpers. Kept as plain functions (not methods) so
# each repository class stays focused on the query it runs.
# ---------------------------------------------------------------------

def _job_record_from_row(row: asyncpg.Record) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        job=JobDescription.model_validate(row["job"]),
        is_active=row["is_active"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _bug_report_from_row(row: asyncpg.Record) -> BugReportRecord:
    return BugReportRecord(
        bug_id=row["bug_id"],
        reporter_user_id=row["reporter_user_id"],
        title=row["title"],
        description=row["description"],
        severity=BugSeverity(row["severity"]),
        status=BugStatus(row["status"]),
        page=row["page"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _candidate_record_from_row(row: asyncpg.Record) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=row["candidate_id"],
        user_id=row["user_id"],
        resume=ParsedResume.model_validate(row["resume"]),
        used_fallback=row["used_fallback"],
        parse_warning=row["parse_warning"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _application_from_row(row: asyncpg.Record) -> Application:
    return Application(
        application_id=row["application_id"],
        job_id=row["job_id"],
        candidate_id=row["candidate_id"],
        status=ApplicationStatus(row["status"]),
        matching_score=(
            MatchingScore.model_validate(row["matching_score"])
            if row["matching_score"] is not None
            else None
        ),
        semantic_score=row["semantic_score"],
        session_id=row["session_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _session_record_from_row(row: asyncpg.Record) -> SessionRecord:
    return SessionRecord(
        session_id=row["session_id"],
        candidate_id=row["candidate_id"],
        job_id=row["job_id"],
        status=SessionStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        termination_reason=row["termination_reason"],
        questions_asked=row["questions_asked"],
        questions_answered=row["questions_answered"],
        application_id=row["application_id"],
        interview_id=row["interview_id"],
        state=(
            InterviewState.model_validate(row["state"])
            if row["state"] is not None
            else None
        ),
        job_description=(
            JobDescription.model_validate(row["job_description_snapshot"])
            if row["job_description_snapshot"] is not None
            else None
        ),
        parsed_resume=(
            ParsedResume.model_validate(row["parsed_resume_snapshot"])
            if row["parsed_resume_snapshot"] is not None
            else None
        ),
    )


def _transcript_from_row(row: asyncpg.Record) -> InterviewTranscript:
    exchanges = [
        (InterviewQuestion.model_validate(q), InterviewAnswer.model_validate(a))
        for q, a in row["exchanges"]
    ]
    return InterviewTranscript(
        interview_id=row["interview_id"],
        candidate_id=row["candidate_id"],
        job_id=row["job_id"],
        start_time=row["start_time"],
        end_time=row["end_time"],
        duration_seconds=row["duration_seconds"],
        exchanges=exchanges,
        interviewer_name=row["interviewer_name"],
        interview_type=row["interview_type"],
        format=row["format"],
        is_sealed=row["is_sealed"],
        seal_timestamp=row["seal_timestamp"],
        raw_transcript=row["raw_transcript"],
    )


def _evaluation_job_from_row(row: asyncpg.Record) -> EvaluationJob:
    return EvaluationJob(
        evaluation_id=row["evaluation_id"],
        session_id=row["session_id"],
        interview_id=row["interview_id"],
        candidate_id=row["candidate_id"],
        job_id=row["job_id"],
        application_id=row["application_id"],
        status=EvaluationStatus(row["status"]),
        result=(
            CandidateReport.model_validate(row["result"])
            if row["result"] is not None
            else None
        ),
        error=row["error"],
        warnings=list(row["warnings"]) if row["warnings"] else [],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


class PostgresJobRepository(JobRepository):
    """Durable storage for `JobRecord`. See
    repositories/postgres/schema.sql's `jobs` table."""

    def __init__(self, pool: PostgresConnectionPool) -> None:
        self._pool = pool

    async def save(self, record: JobRecord) -> JobRecord:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO jobs (job_id, job, is_active, created_at, updated_at)
                VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (job_id) DO UPDATE
                    SET job = EXCLUDED.job,
                        is_active = EXCLUDED.is_active,
                        updated_at = now()
                RETURNING job_id, job, is_active, created_at, updated_at
                """,
                record.job_id,
                record.job.model_dump(mode="json"),
                record.is_active,
                record.created_at,
            )
        return _job_record_from_row(row)

    async def get(self, job_id: str) -> Optional[JobRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jobs WHERE job_id = $1", job_id)
        return _job_record_from_row(row) if row is not None else None

    async def list_jobs(self, *, include_archived: bool = False) -> List[JobRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM jobs
                WHERE ($1 OR is_active)
                ORDER BY created_at DESC
                """,
                include_archived,
            )
        return [_job_record_from_row(row) for row in rows]

    async def archive(self, job_id: str) -> Optional[JobRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE jobs
                SET is_active = false, updated_at = now()
                WHERE job_id = $1
                RETURNING job_id, job, is_active, created_at, updated_at
                """,
                job_id,
            )
        return _job_record_from_row(row) if row is not None else None


class PostgresCandidateRepository(CandidateRepository):
    """Durable storage for `CandidateRecord`. See
    repositories/postgres/schema.sql's `candidates` table."""

    def __init__(self, pool: PostgresConnectionPool) -> None:
        self._pool = pool

    async def save(self, record: CandidateRecord) -> CandidateRecord:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO candidates
                    (candidate_id, user_id, resume, used_fallback, parse_warning, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, now())
                ON CONFLICT (candidate_id) DO UPDATE
                    SET resume = EXCLUDED.resume,
                        used_fallback = EXCLUDED.used_fallback,
                        parse_warning = EXCLUDED.parse_warning,
                        updated_at = now()
                RETURNING candidate_id, user_id, resume, used_fallback, parse_warning, created_at, updated_at
                """,
                record.candidate_id,
                record.user_id,
                record.resume.model_dump(mode="json"),
                record.used_fallback,
                record.parse_warning,
                record.created_at,
            )
        return _candidate_record_from_row(row)

    async def get(self, candidate_id: str) -> Optional[CandidateRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM candidates WHERE candidate_id = $1", candidate_id
            )
        return _candidate_record_from_row(row) if row is not None else None

    async def list_candidates(self) -> List[CandidateRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM candidates ORDER BY created_at DESC")
        return [_candidate_record_from_row(row) for row in rows]

    async def get_many(self, candidate_ids: List[str]) -> List[CandidateRecord]:
        if not candidate_ids:
            return []
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM candidates WHERE candidate_id = ANY($1::text[])",
                candidate_ids,
            )
        return [_candidate_record_from_row(row) for row in rows]

    async def list_for_user(self, user_id: str) -> List[CandidateRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM candidates WHERE user_id = $1 ORDER BY created_at DESC",
                user_id,
            )
        return [_candidate_record_from_row(row) for row in rows]


class PostgresApplicationRepository(ApplicationRepository):
    """Durable storage for `Application`. See
    repositories/postgres/schema.sql's `applications` table."""

    def __init__(self, pool: PostgresConnectionPool) -> None:
        self._pool = pool

    async def save(self, application: Application) -> Application:
        pool = await self._pool.get()
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO applications
                        (application_id, job_id, candidate_id, status, matching_score,
                         semantic_score, session_id, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
                    ON CONFLICT (application_id) DO UPDATE
                        SET job_id = EXCLUDED.job_id,
                            candidate_id = EXCLUDED.candidate_id,
                            status = EXCLUDED.status,
                            matching_score = EXCLUDED.matching_score,
                            semantic_score = EXCLUDED.semantic_score,
                            session_id = EXCLUDED.session_id,
                            updated_at = now()
                    RETURNING application_id, job_id, candidate_id, status,
                              matching_score, semantic_score, session_id,
                              created_at, updated_at
                    """,
                    application.application_id,
                    application.job_id,
                    application.candidate_id,
                    application.status,
                    (
                        application.matching_score.model_dump(mode="json")
                        if application.matching_score is not None
                        else None
                    ),
                    application.semantic_score,
                    application.session_id,
                    application.created_at,
                )
        except asyncpg.UniqueViolationError as exc:
            # uq_applications_job_candidate - the one duplicate-application
            # rule ApplicationService.apply already checks for; this is the
            # DB-level backstop for the same race the check-then-insert
            # cannot close on its own across multiple processes.
            raise RepositoryError(
                f"an application for job_id={application.job_id!r} "
                f"candidate_id={application.candidate_id!r} already exists"
            ) from exc
        return _application_from_row(row)

    async def get(self, application_id: str) -> Optional[Application]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM applications WHERE application_id = $1", application_id
            )
        return _application_from_row(row) if row is not None else None

    async def get_for_job_and_candidate(
        self, job_id: str, candidate_id: str
    ) -> Optional[Application]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM applications WHERE job_id = $1 AND candidate_id = $2",
                job_id,
                candidate_id,
            )
        return _application_from_row(row) if row is not None else None

    async def list_for_job(self, job_id: str) -> List[Application]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM applications WHERE job_id = $1 ORDER BY created_at DESC",
                job_id,
            )
        return [_application_from_row(row) for row in rows]

    async def list_for_candidate(self, candidate_id: str) -> List[Application]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM applications WHERE candidate_id = $1 ORDER BY created_at DESC",
                candidate_id,
            )
        return [_application_from_row(row) for row in rows]


class PostgresSessionRepository(SessionRepository):
    """Durable storage for `SessionRecord`. See
    repositories/postgres/schema.sql's `sessions` table."""

    def __init__(self, pool: PostgresConnectionPool) -> None:
        self._pool = pool

    async def save(self, record: SessionRecord) -> SessionRecord:
        pool = await self._pool.get()
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO sessions (
                        session_id, interview_id, candidate_id, job_id, application_id,
                        status, termination_reason, questions_asked, questions_answered,
                        state, job_description_snapshot, parsed_resume_snapshot,
                        created_at, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, now())
                    ON CONFLICT (session_id) DO UPDATE SET
                        interview_id = EXCLUDED.interview_id,
                        candidate_id = EXCLUDED.candidate_id,
                        job_id = EXCLUDED.job_id,
                        application_id = EXCLUDED.application_id,
                        status = EXCLUDED.status,
                        termination_reason = EXCLUDED.termination_reason,
                        questions_asked = EXCLUDED.questions_asked,
                        questions_answered = EXCLUDED.questions_answered,
                        state = EXCLUDED.state,
                        job_description_snapshot = EXCLUDED.job_description_snapshot,
                        parsed_resume_snapshot = EXCLUDED.parsed_resume_snapshot,
                        updated_at = now()
                    RETURNING *
                    """,
                    record.session_id,
                    record.interview_id,
                    record.candidate_id,
                    record.job_id,
                    record.application_id,
                    record.status,
                    record.termination_reason,
                    record.questions_asked,
                    record.questions_answered,
                    record.state.model_dump(mode="json") if record.state is not None else None,
                    (
                        record.job_description.model_dump(mode="json")
                        if record.job_description is not None
                        else None
                    ),
                    (
                        record.parsed_resume.model_dump(mode="json")
                        if record.parsed_resume is not None
                        else None
                    ),
                    record.created_at,
                )
        except asyncpg.UniqueViolationError as exc:
            # uq_sessions_interview_id - see the audit's own finding that
            # nothing previously guarded against this; astronomically
            # unlikely in practice since interview_id is uuid4-derived.
            raise RepositoryError(
                f"interview_id={record.interview_id!r} collides with an existing session"
            ) from exc
        return _session_record_from_row(row)

    async def get(self, session_id: str) -> Optional[SessionRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sessions WHERE session_id = $1", session_id
            )
        return _session_record_from_row(row) if row is not None else None

    async def list_for_candidate(self, candidate_id: str) -> List[SessionRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM sessions WHERE candidate_id = $1 ORDER BY created_at DESC",
                candidate_id,
            )
        return [_session_record_from_row(row) for row in rows]

    async def delete(self, session_id: str) -> bool:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM sessions WHERE session_id = $1", session_id
            )
        # asyncpg's execute() returns a tag string like "DELETE 1"/"DELETE 0".
        return result.rsplit(" ", 1)[-1] != "0"

    async def get_many(self, session_ids: List[str]) -> List[SessionRecord]:
        if not session_ids:
            return []
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM sessions WHERE session_id = ANY($1::text[])",
                session_ids,
            )
        return [_session_record_from_row(row) for row in rows]


class PostgresTranscriptRepository(TranscriptRepository):
    """Durable storage for sealed `InterviewTranscript`s. See
    repositories/postgres/schema.sql's `transcripts` table."""

    def __init__(self, pool: PostgresConnectionPool) -> None:
        self._pool = pool

    async def save(self, transcript: InterviewTranscript) -> None:
        if not transcript.is_sealed:
            raise RepositoryError(
                f"Refusing to store an unsealed transcript "
                f"(interview_id={transcript.interview_id!r})"
            )
        pool = await self._pool.get()
        exchanges_json = [
            [q.model_dump(mode="json"), a.model_dump(mode="json")]
            for q, a in transcript.exchanges
        ]
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO transcripts (
                    interview_id, candidate_id, job_id, start_time, end_time,
                    duration_seconds, exchanges, interviewer_name, interview_type,
                    format, is_sealed, seal_timestamp, raw_transcript
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (interview_id) DO UPDATE SET
                    candidate_id = EXCLUDED.candidate_id,
                    job_id = EXCLUDED.job_id,
                    start_time = EXCLUDED.start_time,
                    end_time = EXCLUDED.end_time,
                    duration_seconds = EXCLUDED.duration_seconds,
                    exchanges = EXCLUDED.exchanges,
                    interviewer_name = EXCLUDED.interviewer_name,
                    interview_type = EXCLUDED.interview_type,
                    format = EXCLUDED.format,
                    is_sealed = EXCLUDED.is_sealed,
                    seal_timestamp = EXCLUDED.seal_timestamp,
                    raw_transcript = EXCLUDED.raw_transcript
                """,
                transcript.interview_id,
                transcript.candidate_id,
                transcript.job_id,
                transcript.start_time,
                transcript.end_time,
                transcript.duration_seconds,
                exchanges_json,
                transcript.interviewer_name,
                transcript.interview_type,
                transcript.format,
                transcript.is_sealed,
                transcript.seal_timestamp,
                transcript.raw_transcript,
            )

    async def get(self, interview_id: str) -> Optional[InterviewTranscript]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM transcripts WHERE interview_id = $1", interview_id
            )
        return _transcript_from_row(row) if row is not None else None

    async def get_for_candidate(
        self, candidate_id: str, job_id: str
    ) -> List[InterviewTranscript]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM transcripts WHERE candidate_id = $1 AND job_id = $2",
                candidate_id,
                job_id,
            )
        return [_transcript_from_row(row) for row in rows]


class PostgresEvaluationRepository(EvaluationRepository):
    """Durable storage for `EvaluationJob`. See
    repositories/postgres/schema.sql's `evaluations` table."""

    def __init__(self, pool: PostgresConnectionPool) -> None:
        self._pool = pool

    async def save(self, job: EvaluationJob) -> EvaluationJob:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO evaluations (
                    evaluation_id, session_id, interview_id, candidate_id, job_id,
                    application_id, status, result, error, warnings,
                    created_at, updated_at, started_at, completed_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now(), $12, $13)
                ON CONFLICT (evaluation_id) DO UPDATE SET
                    session_id = EXCLUDED.session_id,
                    interview_id = EXCLUDED.interview_id,
                    candidate_id = EXCLUDED.candidate_id,
                    job_id = EXCLUDED.job_id,
                    application_id = EXCLUDED.application_id,
                    status = EXCLUDED.status,
                    result = EXCLUDED.result,
                    error = EXCLUDED.error,
                    warnings = EXCLUDED.warnings,
                    updated_at = now(),
                    started_at = EXCLUDED.started_at,
                    completed_at = EXCLUDED.completed_at
                RETURNING *
                """,
                job.evaluation_id,
                job.session_id,
                job.interview_id,
                job.candidate_id,
                job.job_id,
                job.application_id,
                job.status,
                job.result.model_dump(mode="json") if job.result is not None else None,
                job.error,
                job.warnings,
                job.created_at,
                job.started_at,
                job.completed_at,
            )
        return _evaluation_job_from_row(row)

    async def get(self, evaluation_id: str) -> Optional[EvaluationJob]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM evaluations WHERE evaluation_id = $1", evaluation_id
            )
        return _evaluation_job_from_row(row) if row is not None else None

    async def get_for_session(self, session_id: str) -> Optional[EvaluationJob]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM evaluations WHERE session_id = $1", session_id
            )
        return _evaluation_job_from_row(row) if row is not None else None

    async def create_if_absent_for_session(self, job: EvaluationJob) -> EvaluationJob:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            # ON CONFLICT (session_id) DO NOTHING is the atomic primitive:
            # PostgreSQL enforces the uq/unique index at the statement
            # level, so a concurrent insert for the same session_id from
            # another process either blocks until the first commits (then
            # conflicts) or the first one rolls back (then this one wins) -
            # there is no window where two rows for one session_id can both
            # exist, matching the interface's "existing wins" contract.
            row = await conn.fetchrow(
                """
                INSERT INTO evaluations (
                    evaluation_id, session_id, interview_id, candidate_id, job_id,
                    application_id, status, result, error, warnings,
                    created_at, updated_at, started_at, completed_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now(), $12, $13)
                ON CONFLICT (session_id) DO NOTHING
                RETURNING *
                """,
                job.evaluation_id,
                job.session_id,
                job.interview_id,
                job.candidate_id,
                job.job_id,
                job.application_id,
                job.status,
                job.result.model_dump(mode="json") if job.result is not None else None,
                job.error,
                job.warnings,
                job.created_at,
                job.started_at,
                job.completed_at,
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT * FROM evaluations WHERE session_id = $1", job.session_id
                )
        return _evaluation_job_from_row(row)

    async def get_many_for_sessions(self, session_ids: List[str]) -> Dict[str, EvaluationJob]:
        if not session_ids:
            return {}
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM evaluations WHERE session_id = ANY($1::text[])",
                session_ids,
            )
        return {row["session_id"]: _evaluation_job_from_row(row) for row in rows}


__all__ = [
    "PostgresApplicationRepository",
    "PostgresCandidateRepository",
    "PostgresEvaluationRepository",
    "PostgresJobRepository",
    "PostgresSessionRepository",
    "PostgresTranscriptRepository",
]


def _rubric_from_row(row: asyncpg.Record) -> JobRubric:
    """Rebuild a JobRubric from its jsonb payload.

    status/version live in their own columns so they can be constrained and
    queried, but the jsonb payload is the source of truth for the object, so
    the columns are re-applied onto it here rather than trusted to agree.
    """
    return JobRubric.model_validate(
        {**row["rubric"], "status": row["status"], "version": row["version"]}
    )


class PostgresRubricRepository(RubricRepository):
    """Durable storage for `JobRubric`. See
    repositories/postgres/schema.sql's `job_rubrics` table."""

    def __init__(self, pool: PostgresConnectionPool) -> None:
        self._pool = pool

    async def save(self, rubric: JobRubric) -> JobRubric:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO job_rubrics (rubric_id, job_id, version, status, rubric)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (rubric_id) DO UPDATE
                    SET status = EXCLUDED.status,
                        rubric = EXCLUDED.rubric,
                        updated_at = now()
                RETURNING rubric_id, job_id, version, status, rubric
                """,
                rubric.rubric_id,
                rubric.job_id,
                rubric.version,
                rubric.status.value,
                rubric.model_dump(mode="json"),
            )
        return _rubric_from_row(row)

    async def get(self, rubric_id: str) -> Optional[JobRubric]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM job_rubrics WHERE rubric_id = $1", rubric_id
            )
        return _rubric_from_row(row) if row is not None else None

    async def get_approved_for_job(self, job_id: str) -> Optional[JobRubric]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM job_rubrics WHERE job_id = $1 AND status = 'approved'",
                job_id,
            )
        return _rubric_from_row(row) if row is not None else None

    async def list_versions_for_job(self, job_id: str) -> List[JobRubric]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM job_rubrics WHERE job_id = $1 ORDER BY version",
                job_id,
            )
        return [_rubric_from_row(row) for row in rows]

    async def approve(self, rubric_id: str) -> JobRubric:
        """Approve a rubric, superseding the job's previous approved version.

        Re-runs the approval gate here as well as at the API layer: a
        malformed rubric must not become active by bypassing a route. The
        supersede and the approve happen in one transaction, because the
        partial unique index would otherwise reject the second write and
        leave the job with no approved rubric at all.
        """
        rubric = await self.get(rubric_id)
        if rubric is None:
            raise KeyError(f"Rubric {rubric_id} not found")

        violations = rubric.validate_approvable()
        if violations:
            raise ValueError(f"Rubric {rubric_id} is not approvable: {violations}")

        pool = await self._pool.get()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE job_rubrics SET status = 'superseded', updated_at = now()
                    WHERE job_id = $1 AND status = 'approved' AND rubric_id <> $2
                    """,
                    rubric.job_id,
                    rubric_id,
                )
                row = await conn.fetchrow(
                    """
                    UPDATE job_rubrics SET status = 'approved', updated_at = now()
                    WHERE rubric_id = $1
                    RETURNING rubric_id, job_id, version, status, rubric
                    """,
                    rubric_id,
                )
        return _rubric_from_row(row)


class PostgresBugReportRepository(BugReportRepository):
    """Durable storage for `BugReportRecord`. See
    repositories/postgres/schema.sql's `bug_reports` table."""

    def __init__(self, pool: PostgresConnectionPool) -> None:
        self._pool = pool

    async def save(self, record: BugReportRecord) -> BugReportRecord:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO bug_reports
                    (bug_id, reporter_user_id, title, description, severity, status, page, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
                ON CONFLICT (bug_id) DO UPDATE
                    SET title = EXCLUDED.title,
                        description = EXCLUDED.description,
                        severity = EXCLUDED.severity,
                        status = EXCLUDED.status,
                        page = EXCLUDED.page,
                        updated_at = now()
                RETURNING bug_id, reporter_user_id, title, description, severity, status, page, created_at, updated_at
                """,
                record.bug_id,
                record.reporter_user_id,
                record.title,
                record.description,
                record.severity.value,
                record.status.value,
                record.page,
                record.created_at,
            )
        return _bug_report_from_row(row)

    async def get(self, bug_id: str) -> Optional[BugReportRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM bug_reports WHERE bug_id = $1", bug_id
            )
        return _bug_report_from_row(row) if row is not None else None

    async def list_all(self) -> List[BugReportRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM bug_reports ORDER BY created_at DESC")
        return [_bug_report_from_row(row) for row in rows]
