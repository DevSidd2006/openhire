"""
TEMPORARY in-process adapters for the persistence contracts.

    ############################################################
    #  REPLACE THIS MODULE. It is the only stub in the backend. #
    ############################################################

Every implementation here keeps data in a plain dict inside one Python
process. That means:

  * a restart loses every session record and every sealed transcript;
  * a second worker process sees none of the first one's data;
  * nothing here is durable, transactional, or queryable.

They exist so the service layer, the routes and the tests can be written
against the real contracts *now*, while the database schema is being
designed by its owner. They are not a design proposal for that schema and
they must not be deployed as-is.

How to replace them
-------------------
Write `repositories/<backend>.py` implementing `SessionRepository` and
`TranscriptRepository` from repositories/interfaces.py, then change the two
construction lines in `core/container.py`. Nothing else in the backend
imports this module - not the services, not the routes, not the domain -
so the swap touches exactly one file.

How to tell whether they are live
---------------------------------
`ServiceContainer.persistence_is_ephemeral` is True while these are in use,
the startup banner logs a warning naming this module, and `/health` reports
`"persistence": "ephemeral"`. A stub this consequential should never be
possible to run in production without noticing.
"""
from __future__ import annotations
from schemas.rubric import JobRubric, RubricStatus

import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional

from repositories.interfaces import (
    RubricRepository,
    Application,
    ApplicationRepository,
    BugReportRecord,
    BugReportRepository,
    CandidateRecord,
    CandidateRepository,
    EvaluationJob,
    EvaluationRepository,
    JobRecord,
    JobRepository,
    RepositoryError,
    SessionRecord,
    SessionRepository,
    TranscriptRepository,
    UserRecord,
    UserRepository,
)
from schemas.interview import InterviewTranscript

# Named so it can be asserted on and logged, rather than described in prose
# that drifts away from the code.
EPHEMERAL_BACKEND_NAME = "in-memory (repositories/memory.py)"


class InMemorySessionRepository(SessionRepository):
    """Dict-backed `SessionRepository`. TEMPORARY - see module docstring.

    Guarded by an `asyncio.Lock` for the same reason
    `api.registry.SessionRegistry` is: dict operations are already atomic
    under the GIL, but stating the concurrency guarantee explicitly means it
    survives a future implementation change and matches the behaviour a real
    repository will have.

    Chunk 3: `SessionRecord` now embeds a full `InterviewState` (nested
    mutable lists/dicts) plus a `JobDescription`/`ParsedResume` snapshot -
    `save`/`get` deep-copy on the way in and out for the exact reason
    `InMemoryTranscriptRepository` already does: a real database serialises
    at this boundary, so a caller mutating its own object afterwards (or the
    object handed back by `get`) must never be able to retroactively change
    what was "persisted".
    """

    def __init__(self) -> None:
        self._records: Dict[str, SessionRecord] = {}
        self._lock = asyncio.Lock()

    async def save(self, record: SessionRecord) -> SessionRecord:
        async with self._lock:
            existing = self._records.get(record.session_id)
            # Preserve the original creation time across updates; the caller
            # rebuilds the record from the runner on every transition and
            # would otherwise reset created_at on each write.
            created_at = existing.created_at if existing else record.created_at
            stored = record.model_copy(
                deep=True,
                update={"created_at": created_at, "updated_at": datetime.now(timezone.utc)},
            )
            self._records[record.session_id] = stored
            return stored.model_copy(deep=True)

    async def get(self, session_id: str) -> Optional[SessionRecord]:
        async with self._lock:
            record = self._records.get(session_id)
        return record.model_copy(deep=True) if record is not None else None

    async def list_for_candidate(self, candidate_id: str) -> List[SessionRecord]:
        async with self._lock:
            matches = [
                r.model_copy(deep=True)
                for r in self._records.values() if r.candidate_id == candidate_id
            ]
        return sorted(matches, key=lambda r: r.created_at, reverse=True)

    async def delete(self, session_id: str) -> bool:
        async with self._lock:
            return self._records.pop(session_id, None) is not None

    async def get_many(self, session_ids: List[str]) -> List[SessionRecord]:
        wanted = set(session_ids)
        async with self._lock:
            matches = [
                r.model_copy(deep=True)
                for sid, r in self._records.items() if sid in wanted
            ]
        return matches


class InMemoryTranscriptRepository(TranscriptRepository):
    """Dict-backed `TranscriptRepository`. TEMPORARY - see module docstring."""

    def __init__(self) -> None:
        self._transcripts: Dict[str, InterviewTranscript] = {}
        self._lock = asyncio.Lock()

    async def save(self, transcript: InterviewTranscript) -> None:
        # Enforced here and not only documented on the interface: an unsealed
        # transcript reaching storage would let a half-finished interview be
        # scored as a complete one.
        if not transcript.is_sealed:
            raise RepositoryError(
                f"Refusing to store an unsealed transcript "
                f"(interview_id={transcript.interview_id!r})"
            )
        async with self._lock:
            # Deep copy so a caller mutating its own object afterwards cannot
            # retroactively change what was "persisted" - a real database
            # would serialise at this point, and the stub must not be more
            # permissive than the thing it stands in for.
            self._transcripts[transcript.interview_id] = transcript.model_copy(deep=True)

    async def get(self, interview_id: str) -> Optional[InterviewTranscript]:
        async with self._lock:
            stored = self._transcripts.get(interview_id)
        return stored.model_copy(deep=True) if stored is not None else None

    async def get_for_candidate(
        self, candidate_id: str, job_id: str
    ) -> List[InterviewTranscript]:
        async with self._lock:
            matches = [
                t.model_copy(deep=True)
                for t in self._transcripts.values()
                if t.candidate_id == candidate_id and t.job_id == job_id
            ]
        return matches


class InMemoryJobRepository(JobRepository):
    """Dict-backed `JobRepository`. TEMPORARY - see module docstring."""

    def __init__(self) -> None:
        self._jobs: Dict[str, JobRecord] = {}
        self._lock = asyncio.Lock()

    async def save(self, record: JobRecord) -> JobRecord:
        async with self._lock:
            existing = self._jobs.get(record.job_id)
            created_at = existing.created_at if existing else record.created_at
            stored = record.model_copy(
                update={"created_at": created_at, "updated_at": datetime.now(timezone.utc)}
            )
            self._jobs[record.job_id] = stored
            return stored

    async def get(self, job_id: str) -> Optional[JobRecord]:
        async with self._lock:
            return self._jobs.get(job_id)

    async def list_jobs(self, *, include_archived: bool = False) -> List[JobRecord]:
        async with self._lock:
            matches = [
                r for r in self._jobs.values() if include_archived or r.is_active
            ]
        return sorted(matches, key=lambda r: r.created_at, reverse=True)

    async def archive(self, job_id: str) -> Optional[JobRecord]:
        async with self._lock:
            existing = self._jobs.get(job_id)
            if existing is None:
                return None
            archived = existing.model_copy(
                update={"is_active": False, "updated_at": datetime.now(timezone.utc)}
            )
            self._jobs[job_id] = archived
            return archived


class InMemoryCandidateRepository(CandidateRepository):
    """Dict-backed `CandidateRepository`. TEMPORARY - see module docstring."""

    def __init__(self) -> None:
        self._candidates: Dict[str, CandidateRecord] = {}
        self._lock = asyncio.Lock()

    async def save(self, record: CandidateRecord) -> CandidateRecord:
        async with self._lock:
            existing = self._candidates.get(record.candidate_id)
            created_at = existing.created_at if existing else record.created_at
            stored = record.model_copy(
                update={"created_at": created_at, "updated_at": datetime.now(timezone.utc)}
            )
            self._candidates[record.candidate_id] = stored
            return stored

    async def get(self, candidate_id: str) -> Optional[CandidateRecord]:
        async with self._lock:
            return self._candidates.get(candidate_id)

    async def list_candidates(self) -> List[CandidateRecord]:
        async with self._lock:
            matches = list(self._candidates.values())
        return sorted(matches, key=lambda r: r.created_at, reverse=True)

    async def get_many(self, candidate_ids: List[str]) -> List[CandidateRecord]:
        wanted = set(candidate_ids)
        async with self._lock:
            return [c for cid, c in self._candidates.items() if cid in wanted]

    async def list_for_user(self, user_id: str) -> List[CandidateRecord]:
        async with self._lock:
            candidates = [c for c in self._candidates.values() if c.user_id == user_id]
        return sorted(candidates, key=lambda r: r.created_at, reverse=True)


class InMemoryApplicationRepository(ApplicationRepository):
    """Dict-backed `ApplicationRepository`. TEMPORARY - see module docstring."""

    def __init__(self) -> None:
        self._applications: Dict[str, Application] = {}
        self._lock = asyncio.Lock()

    async def save(self, application: Application) -> Application:
        async with self._lock:
            stored = application.model_copy(update={"updated_at": datetime.now(timezone.utc)})
            self._applications[application.application_id] = stored
            return stored

    async def get(self, application_id: str) -> Optional[Application]:
        async with self._lock:
            return self._applications.get(application_id)

    async def get_for_job_and_candidate(
        self, job_id: str, candidate_id: str
    ) -> Optional[Application]:
        async with self._lock:
            for application in self._applications.values():
                if application.job_id == job_id and application.candidate_id == candidate_id:
                    return application
        return None

    async def list_for_job(self, job_id: str) -> List[Application]:
        async with self._lock:
            matches = [a for a in self._applications.values() if a.job_id == job_id]
        return sorted(matches, key=lambda a: a.created_at, reverse=True)

    async def list_for_candidate(self, candidate_id: str) -> List[Application]:
        async with self._lock:
            matches = [a for a in self._applications.values() if a.candidate_id == candidate_id]
        return sorted(matches, key=lambda a: a.created_at, reverse=True)


class InMemoryEvaluationRepository(EvaluationRepository):
    """Dict-backed `EvaluationRepository`. TEMPORARY - see module docstring.

    `EvaluationJob.result` embeds a `CandidateReport` (nested mutable
    lists/evidence) - `save`/`get`/`get_for_session` deep-copy for the same
    reason `InMemorySessionRepository` does for its own embedded
    `InterviewState`.
    """

    def __init__(self) -> None:
        self._jobs: Dict[str, EvaluationJob] = {}
        # A second index (session_id -> evaluation_id) is what makes
        # create_if_absent_for_session O(1) and, more importantly, lets it
        # hold ONE lock across the whole check-then-insert instead of
        # scanning - the exact atomicity Chunk 4 Step 6 requires.
        self._by_session: Dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def save(self, job: EvaluationJob) -> EvaluationJob:
        async with self._lock:
            existing = self._jobs.get(job.evaluation_id)
            created_at = existing.created_at if existing else job.created_at
            stored = job.model_copy(
                deep=True,
                update={"created_at": created_at, "updated_at": datetime.now(timezone.utc)},
            )
            self._jobs[job.evaluation_id] = stored
            self._by_session[job.session_id] = job.evaluation_id
            return stored.model_copy(deep=True)

    async def get(self, evaluation_id: str) -> Optional[EvaluationJob]:
        async with self._lock:
            job = self._jobs.get(evaluation_id)
        return job.model_copy(deep=True) if job is not None else None

    async def get_for_session(self, session_id: str) -> Optional[EvaluationJob]:
        async with self._lock:
            evaluation_id = self._by_session.get(session_id)
            job = self._jobs.get(evaluation_id) if evaluation_id else None
        return job.model_copy(deep=True) if job is not None else None

    async def create_if_absent_for_session(self, job: EvaluationJob) -> EvaluationJob:
        async with self._lock:
            existing_id = self._by_session.get(job.session_id)
            if existing_id is not None:
                existing = self._jobs.get(existing_id)
                if existing is not None:
                    return existing.model_copy(deep=True)
            stored = job.model_copy(deep=True)
            self._jobs[job.evaluation_id] = stored
            self._by_session[job.session_id] = job.evaluation_id
            return stored.model_copy(deep=True)

    async def get_many_for_sessions(self, session_ids: List[str]) -> Dict[str, EvaluationJob]:
        wanted = set(session_ids)
        async with self._lock:
            result = {}
            for session_id in wanted:
                evaluation_id = self._by_session.get(session_id)
                job = self._jobs.get(evaluation_id) if evaluation_id else None
                if job is not None:
                    result[session_id] = job.model_copy(deep=True)
        return result


class InMemoryUserRepository(UserRepository):
    """Dict-backed `UserRepository`. TEMPORARY - see module docstring.

    Stores user accounts in memory with basic email/id lookups.
    """

    def __init__(self) -> None:
        self._users: Dict[str, UserRecord] = {}
        self._email_index: Dict[str, str] = {}  # email -> user_id
        self._lock = asyncio.Lock()

    async def save(self, record: UserRecord) -> UserRecord:
        """Insert or update by user_id. Idempotent, preserves created_at."""
        async with self._lock:
            existing = self._users.get(record.user_id)
            created_at = existing.created_at if existing else record.created_at
            stored = record.model_copy(
                update={"created_at": created_at, "updated_at": datetime.now(timezone.utc)}
            )
            self._users[record.user_id] = stored
            self._email_index[record.email] = record.user_id
            return stored

    async def get_by_email(self, email: str) -> Optional[UserRecord]:
        """Fetch user by email. Returns None if not found."""
        async with self._lock:
            user_id = self._email_index.get(email)
            return self._users.get(user_id) if user_id else None

    async def get_by_id(self, user_id: str) -> Optional[UserRecord]:
        """Fetch user by user_id. Returns None if not found."""
        async with self._lock:
            return self._users.get(user_id)


__all__ = [
    "EPHEMERAL_BACKEND_NAME",
    "InMemoryApplicationRepository",
    "InMemoryCandidateRepository",
    "InMemoryEvaluationRepository",
    "InMemoryJobRepository",
    "InMemorySessionRepository",
    "InMemoryTranscriptRepository",
    "InMemoryUserRepository",
]


class InMemoryRubricRepository(RubricRepository):
    """In-memory rubric storage for tests and local runs."""

    def __init__(self):
        self._rubrics: dict[str, JobRubric] = {}

    async def save(self, rubric: JobRubric) -> JobRubric:
        self._rubrics[rubric.rubric_id] = rubric
        return rubric

    async def get(self, rubric_id: str) -> Optional[JobRubric]:
        return self._rubrics.get(rubric_id)

    async def get_approved_for_job(self, job_id: str) -> Optional[JobRubric]:
        for rubric in self._rubrics.values():
            if rubric.job_id == job_id and rubric.status is RubricStatus.APPROVED:
                return rubric
        return None

    async def list_versions_for_job(self, job_id: str) -> list[JobRubric]:
        return sorted(
            (r for r in self._rubrics.values() if r.job_id == job_id),
            key=lambda r: r.version,
        )

    async def approve(self, rubric_id: str) -> JobRubric:
        rubric = self._rubrics[rubric_id]
        violations = rubric.validate_approvable()
        if violations:
            raise ValueError(f"Rubric {rubric_id} is not approvable: {violations}")

        for other in list(self._rubrics.values()):
            if other.job_id == rubric.job_id and other.status is RubricStatus.APPROVED:
                self._rubrics[other.rubric_id] = other.model_copy(
                    update={"status": RubricStatus.SUPERSEDED}
                )

        approved = rubric.model_copy(update={"status": RubricStatus.APPROVED})
        self._rubrics[rubric_id] = approved
        return approved


class InMemoryBugReportRepository(BugReportRepository):
    """Dict-backed `BugReportRepository`. TEMPORARY - see module docstring."""

    def __init__(self) -> None:
        self._reports: Dict[str, BugReportRecord] = {}
        self._lock = asyncio.Lock()

    async def save(self, record: BugReportRecord) -> BugReportRecord:
        async with self._lock:
            existing = self._reports.get(record.bug_id)
            created_at = existing.created_at if existing else record.created_at
            stored = record.model_copy(
                update={"created_at": created_at, "updated_at": datetime.now(timezone.utc)}
            )
            self._reports[record.bug_id] = stored
            return stored

    async def get(self, bug_id: str) -> Optional[BugReportRecord]:
        async with self._lock:
            return self._reports.get(bug_id)

    async def list_all(self) -> List[BugReportRecord]:
        async with self._lock:
            reports = list(self._reports.values())
        return sorted(reports, key=lambda r: r.created_at, reverse=True)
