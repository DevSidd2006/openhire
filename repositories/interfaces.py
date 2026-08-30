"""
Data-access contracts.

Read the distinction below before adding anything to this file, because it
is the whole reason the persistence boundary can be drawn now, before the
database exists.

A live interview involves two very different kinds of state:

  1. **Runtime state** - the `InterviewSessionRunner` object itself. It owns
     an `asyncio.Lock`, an in-flight LLM client, and a per-question
     idempotency cache. It is a *process object*; it cannot be serialised
     into a row and it is not what a database is for. It is addressed by
     `SessionRuntimeRegistry`, which stays in-process even after the
     database lands.

  2. **Durable state** - facts about the session that must outlive the
     process: which candidate and job it belongs to, what state it reached,
     when it started and ended, and the sealed `InterviewTranscript` that
     the evaluation pipeline consumes. This is what `SessionRepository` and
     `TranscriptRepository` address, and this is the part the database team
     will implement.

Conflating the two is the mistake this file exists to prevent: a repository
that hands back live runner objects looks like persistence but can never be
backed by one.

Chunk 3: session restoration
-----------------------------
A runtime `InterviewSessionRunner` can always be rebuilt from durable state
alone - that is the whole point of this boundary, and it is what makes a
process restart survivable. `SessionRecord.state` now carries the runner's
COMPLETE `InterviewState` (current question, exchanges, competency scores,
everything the P3 adaptive engine tracks) rather than only a
questions_asked/questions_answered summary, and
`SessionRecord.job_description`/`parsed_resume` carry the runner's own
private copies of the job/resume it was constructed with.

That second point matters more than it looks: the runner has NEVER re-read
`JobDescription`/`ParsedResume` after construction (they are plain
constructor arguments), so persisting anything else - e.g. re-resolving
them from `JobRepository`/`CandidateRepository` at restore time - would let
a job edited or archived AFTER an interview began retroactively change what
an already-in-progress (or already-completed) interview is being scored
against. Snapshotting the runner's own copies is what makes restoration
byte-for-byte faithful to the live session, and incidentally means
restoration never depends on the job/candidate repositories at all - see
`services/interview_service.py:_restore_runner` and
`utils/interview_session.py:InterviewSessionRunner.rehydrate`.

The unavoidable cost: a `SessionRecord` is now a meaningfully larger row
(it embeds two other domain objects and the full interview state). Whether
the eventual database stores that as one JSON(B) column, normalizes it into
several tables, or something else entirely is exactly the kind of decision
left to the database team - `SessionRepository`'s method signatures are
unchanged (still `save`/`get`/`list_for_candidate`/`delete`); only the
shape of the record they carry grew.

What this file is NOT
---------------------
It is not a schema. `SessionRecord` is a transport-neutral DTO describing
data the backend *already has* today (an InterviewSessionRunner's
candidate_id, job_id and SessionStatus); it is not a table definition, not
an ORM model, and its field names carry no requirement for the eventual
columns. The database owner is free to model this however they choose - the
only commitment is that some implementation can satisfy these method
signatures. Only two repositories exist because only two are needed by code
that exists; job, candidate, score and recruiter repositories belong to the
chunk that introduces those endpoints.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from schemas.application import Application
from schemas.interview import InterviewState, InterviewTranscript
from schemas.job import JobDescription
from schemas.resume import ParsedResume
from schemas.scoring import CandidateReport
from utils.interview_session import (
    InterviewSessionError,
    InterviewSessionRunner,
    SessionStatus,
)


class RepositoryError(Exception):
    """A data-access operation failed for an infrastructure reason.

    Deliberately distinct from `core.errors.NotFoundError` and friends: a
    repository must not decide what HTTP status its failure deserves. The
    service layer translates. A "row is absent" outcome is expressed by
    returning `None`, never by raising - absence is a normal result, not an
    error, and making callers handle it explicitly is what stops a missing
    record from being silently treated as an empty one.
    """


class SessionRecord(BaseModel):
    """Durable facts about one interview session - and, as of Chunk 3,
    everything needed to RESTORE it.

    Every field is something the backend can already produce today from an
    `InterviewSessionRunner`; nothing is speculative. `updated_at` is set by
    the repository implementation, not the caller, so it cannot drift.

    `interview_id`/`state`/`job_description`/`parsed_resume` are all
    `Optional` even though `from_runner` always populates them, so that a
    record built by hand with only the original Chunk 1 fields (as several
    existing tests still do) remains valid - adding them is a pure
    additive change to this model, not a breaking one.
    """

    model_config = ConfigDict(frozen=True)

    session_id: str
    candidate_id: str
    job_id: str
    status: SessionStatus
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None
    termination_reason: Optional[str] = None
    questions_asked: int = 0
    questions_answered: int = 0

    # Chunk 2: which application this session belongs to, if any. Set once,
    # at creation (services/interview_service.py:create_session) - an
    # application's linked session never changes afterward, so there is no
    # "keep two copies in sync" risk despite `Application.session_id`
    # (schemas/application.py) recording the SAME link in the other
    # direction.
    application_id: Optional[str] = None

    # Chunk 3: the identifiers/state needed to rebuild a live runner.
    #
    # `interview_id` is kept as its own field (rather than only reachable
    # via `state.interview_id`) so a caller can look up this session's
    # transcript in TranscriptRepository without needing to unpack `state`.
    #
    # `state` is the runner's COMPLETE `InterviewState` - current question,
    # every exchange, competency scores/confidence/signals, everything the
    # P3 adaptive engine tracks - not a derived summary. This is the actual
    # restoration payload; `questions_asked`/`questions_answered` above are
    # kept for cheap summary access without unpacking it, but are now
    # redundant with `state.questions_asked`/`state.questions_answered`.
    #
    # `job_description`/`parsed_resume` are the runner's OWN private copies
    # from construction time - see this file's module docstring for why
    # these must never be re-resolved from JobRepository/CandidateRepository
    # at restore time instead.
    interview_id: Optional[str] = None
    state: Optional[InterviewState] = None
    job_description: Optional[JobDescription] = None
    parsed_resume: Optional[ParsedResume] = None

    @classmethod
    def from_runner(cls, session_id: str, runner: InterviewSessionRunner) -> "SessionRecord":
        """Project a live runner onto its durable, RESTORABLE record.

        Read-only: it calls only the runner's existing accessors and never
        mutates or advances the session. This is the single adapter between
        the interview engine and the persistence boundary - the engine
        itself stays entirely unaware that persistence exists.

        A CREATED session has no `InterviewState` yet and `get_state()`
        correctly raises for it; that is a valid moment to record a session,
        so `state` and the progress counters simply stay at their empty/zero
        defaults rather than the projection failing or inventing a state.
        In practice this never happens for a saved record today - the
        service layer only ever persists a session after `start()` has
        already produced one - but the projection itself must not assume
        that.

        `application_id` is deliberately NOT set here: it is not something
        the runner knows about at all (an application is a service-layer
        concept - see schemas/application.py), so it is left for the
        caller to attach via `model_copy(update={"application_id": ...})`,
        exactly as services/interview_service.py's `_persist_record` does.
        """
        try:
            state = runner.get_state()
        except InterviewSessionError:
            state = None
        return cls(
            session_id=session_id,
            candidate_id=runner.candidate_id,
            job_id=runner.job_description.job_id,
            interview_id=runner.interview_id,
            status=runner.status,
            state=state,
            job_description=runner.job_description,
            parsed_resume=runner.parsed_resume,
            termination_reason=state.termination_reason if state else None,
            questions_asked=state.questions_asked if state else 0,
            questions_answered=state.questions_answered if state else 0,
        )


@runtime_checkable
class SessionRuntimeRegistry(Protocol):
    """Where live `InterviewSessionRunner` objects are found between
    requests.

    A `Protocol`, not an ABC, on purpose: `api.registry.SessionRegistry`
    already implements exactly these five methods and predates this package.
    Structural typing lets it satisfy the contract with no change to its
    class hierarchy, so the existing `app.state.registry` seam that
    tests/test_api.py relies on keeps working verbatim.

    Implementations raise `api.registry.SessionNotFoundError` from
    `get_session` - retained rather than replaced, because the existing 404
    handler and its tests are built on it.

    `restore_session` (Chunk 3) is distinct from `create_session`:
    `create_session` always mints a brand-new session_id, which is correct
    for a genuinely new session but wrong for putting an already-known
    session_id's rehydrated runner back into the registry after a restart -
    see `api.registry.SessionRegistry.restore_session` and
    `services/interview_service.py:_restore_runner`.
    """

    async def create_session(self, runner: InterviewSessionRunner) -> str: ...

    async def get_session(self, session_id: str) -> InterviewSessionRunner: ...

    async def restore_session(
        self, session_id: str, runner: InterviewSessionRunner
    ) -> InterviewSessionRunner: ...

    async def remove_session(self, session_id: str) -> None: ...

    async def session_count(self) -> int: ...


class SessionRepository(ABC):
    """Durable storage for `SessionRecord`.

    An ABC rather than a Protocol because, unlike the runtime registry,
    there is no pre-existing implementation to accommodate - and an explicit
    base class means a partially-implemented database adapter fails at
    construction rather than at the first call in production.
    """

    @abstractmethod
    async def save(self, record: SessionRecord) -> SessionRecord:
        """Insert or update by `session_id`. Returns the stored record with
        `updated_at` populated. Idempotent: saving the same record twice is
        not an error, because the service layer writes on every state
        transition and a retried request must not create a duplicate."""

    @abstractmethod
    async def get(self, session_id: str) -> Optional[SessionRecord]:
        """The record, or None if there is none. Never raises for absence."""

    @abstractmethod
    async def list_for_candidate(self, candidate_id: str) -> list[SessionRecord]:
        """Every session belonging to one candidate, newest first.

        Scoped by candidate rather than offering an unbounded "list all":
        an unscoped listing is how one candidate's interview ends up visible
        to another. Recruiter-facing, cross-candidate queries need an
        authorisation decision behind them and belong with the recruiter
        endpoints, not here.
        """

    @abstractmethod
    async def delete(self, session_id: str) -> bool:
        """Remove the record. Returns whether one existed."""

    @abstractmethod
    async def get_many(self, session_ids: list[str]) -> list[SessionRecord]:
        """Every record found for the given ids, in no particular order -
        missing ids are simply absent from the result, never an error.

        Chunk 5: exists so a recruiter-facing listing (e.g.
        `RecruiterService.list_job_applications`, which already has a batch
        of `Application.session_id`s in hand) can resolve interview status
        for N applications in one call instead of N - the literal N+1
        pattern the recruiter chunk was explicitly told to avoid. The
        in-memory implementation is a simple loop; a real database
        implements this as a single `WHERE id IN (...)` query.
        """


class TranscriptRepository(ABC):
    """Durable storage for sealed `InterviewTranscript`s.

    This is the gap that most justifies introducing the boundary now: today
    a completed interview's transcript exists only inside the runner object
    held in a process-local dict, so a restart destroys the entire record of
    the interview, and tests/test_api.py has to reach through
    `app.state.registry` to get at one. Both the evaluation pipeline and
    every recruiter-facing view need it from storage.

    `InterviewTranscript` (schemas/interview.py) is used as-is. Introducing a
    second transcript representation for persistence is exactly what
    utils/interview_session.py's docstring warns against.
    """

    @abstractmethod
    async def save(self, transcript: InterviewTranscript) -> None:
        """Persist a SEALED transcript.

        Implementations must reject an unsealed one (`is_sealed is False`)
        with `RepositoryError`. An interview that is still in progress has no
        final content, and storing a partial transcript is how a half-finished
        interview gets scored as if it were complete.
        """

    @abstractmethod
    async def get(self, interview_id: str) -> Optional[InterviewTranscript]:
        """The transcript, or None."""

    @abstractmethod
    async def get_for_candidate(self, candidate_id: str, job_id: str) -> list[InterviewTranscript]:
        """Transcripts for one candidate on one job.

        Both keys are required, not optional filters: this is the query the
        evaluation pipeline actually makes (`PipelineState.interview_transcripts`
        is keyed by candidate for a single job), and requiring both means the
        method cannot accidentally be used as a cross-job dump.
        """


class JobRecord(BaseModel):
    """Durable storage record for one job posting.

    Wraps `JobDescription` (schemas/job.py) rather than re-declaring its
    fields - the same "reuse the existing schema, add only the bookkeeping
    it doesn't carry" pattern `SessionRecord` above already established.
    `JobDescription` has no notion of "posted vs. archived" or of when it
    was stored; those are repository-layer facts, not domain facts, so they
    live here instead of being added to `JobDescription` itself.
    """

    model_config = ConfigDict(frozen=False)

    job_id: str
    job: JobDescription
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None


class JobRepository(ABC):
    """Durable storage for `JobRecord`."""

    @abstractmethod
    async def save(self, record: JobRecord) -> JobRecord:
        """Insert or update by `job_id`. Idempotent, and preserves the
        original `created_at` across an update - see `SessionRepository.save`
        for the same contract on the interview side."""

    @abstractmethod
    async def get(self, job_id: str) -> Optional[JobRecord]:
        """The record regardless of `is_active` - a recruiter must still be
        able to open an archived job. Never raises for absence."""

    @abstractmethod
    async def list_jobs(self, *, include_archived: bool = False) -> list[JobRecord]:
        """All jobs, newest first. Archived jobs are excluded unless asked
        for, so a default listing reflects postings that are actually open."""

    @abstractmethod
    async def archive(self, job_id: str) -> Optional[JobRecord]:
        """Set `is_active=False`. Returns the updated record, or None if the
        job does not exist. Idempotent: archiving an already-archived job is
        not an error."""


class CandidateRecord(BaseModel):
    """Durable storage record for one candidate.

    Wraps `ParsedResume` (schemas/resume.py) - the resume IS the candidate
    profile everywhere else in this codebase (agents, the interview engine,
    the matching agent all key off `ParsedResume.candidate_id`), so this
    does not introduce a second, competing candidate representation.

    `used_fallback`/`parse_warning` surface
    `ResumeParserAgent`'s own existing tiered failure policy (module
    docstring of agents/resume_parser/agent.py) rather than hiding it: a
    resume parsed via the deterministic fallback is real, storable data, not
    a failure, but a caller is entitled to know the structured extraction
    did not run.
    """

    model_config = ConfigDict(frozen=False)

    candidate_id: str
    resume: ParsedResume
    used_fallback: bool = False
    parse_warning: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None


class CandidateRepository(ABC):
    """Durable storage for `CandidateRecord`.

    No archive/delete method: nothing in the project today calls for
    removing a candidate record, and every consumer (matching, interviews,
    evaluation) needs it to keep existing indefinitely. Add one only when a
    real deletion requirement (e.g. a data-retention policy) shows up.
    """

    @abstractmethod
    async def save(self, record: CandidateRecord) -> CandidateRecord:
        """Insert or update by `candidate_id`. Idempotent; preserves the
        original `created_at` across an update."""

    @abstractmethod
    async def get(self, candidate_id: str) -> Optional[CandidateRecord]:
        """The record, or None. Never raises for absence."""

    @abstractmethod
    async def list_candidates(self) -> list[CandidateRecord]:
        """All candidates, newest first."""

    @abstractmethod
    async def get_many(self, candidate_ids: list[str]) -> list[CandidateRecord]:
        """Every record found for the given ids, in no particular order -
        missing ids are simply absent, never an error. See
        `SessionRepository.get_many`'s docstring for why this exists
        (Chunk 5's recruiter applications-for-a-job listing)."""


class UserRecord(BaseModel):
    """Durable storage record for one user account.

    Stores authentication credentials and account metadata. The password_hash
    field contains the output of a proper password hashing algorithm (e.g.
    bcrypt, scrypt), never plaintext.
    """

    model_config = ConfigDict(frozen=False)

    user_id: str
    email: str
    password_hash: str
    user_type: str  # 'candidate' or 'recruiter'
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None


class UserRepository(ABC):
    """Durable storage for user accounts.

    Handles authentication and account lookup for both candidate and recruiter
    users. The interface is minimal: just the operations required by the
    authentication system, leaving schema/policy decisions to implementers.
    """

    @abstractmethod
    async def save(self, record: UserRecord) -> UserRecord:
        """Insert or update by user_id. Idempotent, preserves created_at."""

    @abstractmethod
    async def get_by_email(self, email: str) -> Optional[UserRecord]:
        """Fetch user by email. Returns None if not found."""

    @abstractmethod
    async def get_by_id(self, user_id: str) -> Optional[UserRecord]:
        """Fetch user by user_id. Returns None if not found."""


class ApplicationRepository(ABC):
    """Durable storage for `Application` (schemas/application.py).

    Stores the domain schema directly - no wrapper - for the same reason
    `TranscriptRepository` stores `InterviewTranscript` directly: it is
    already storage-shaped and there is no live runtime object standing
    between it and the repository.
    """

    @abstractmethod
    async def save(self, application: Application) -> Application:
        """Insert or update by `application_id`. Idempotent."""

    @abstractmethod
    async def get(self, application_id: str) -> Optional[Application]:
        """The application, or None. Never raises for absence."""

    @abstractmethod
    async def get_for_job_and_candidate(
        self, job_id: str, candidate_id: str
    ) -> Optional[Application]:
        """The existing application for this (job, candidate) pair, if any -
        the lookup `ApplicationService.apply` uses to reject a duplicate
        application (at most one application per candidate per job)."""

    @abstractmethod
    async def list_for_job(self, job_id: str) -> list[Application]:
        """Every application against one job - the pool
        `MatchingService`/`ApplicationService` rank and shortlist from."""

    @abstractmethod
    async def list_for_candidate(self, candidate_id: str) -> list[Application]:
        """Every application belonging to one candidate. Scoped, for the
        same isolation reason `SessionRepository.list_for_candidate` is."""


class EvaluationStatus(str, Enum):
    """Lifecycle of one evaluation job (Chunk 4).

    Not modeled on `SessionStatus` or `ApplicationStatus` - neither fits a
    background job (a session is a conversation; an application is a
    decision pipeline) - this is the standard vocabulary for a unit of
    scheduled, possibly-retried work, and nothing already in this project
    represents that concept, so it is not a name chosen blindly.

    PENDING    created, not yet picked up by the dispatcher.
    RUNNING    the agent pipeline is currently executing.
    COMPLETED  a `CandidateReport` was produced and stored in `result`.
    FAILED     a required stage could not produce a result - see
               `EvaluationJob.error`. Retryable (see
               `EvaluationService.retry_evaluation`); PENDING/RUNNING/
               COMPLETED are not (see that service's idempotency policy).
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EvaluationJob(BaseModel):
    """One evaluation run for one sealed interview session.

    Stores the EXISTING `CandidateReport` (schemas/scoring.py) as `result` -
    no new "evaluation result" schema is introduced; `CandidateReport`
    already IS the complete, final per-candidate artifact the existing
    scoring/report agents produce (scores with full per-competency
    evidence, summaries, flags, recommendation). This record only adds the
    job-tracking metadata (status, timing, which session/interview it
    covers) around that existing object.

    Deliberately does NOT re-store the transcript, the job description, or
    the resume: `TranscriptRepository` and `SessionRecord`
    (Chunk 1/3) already hold those as the authoritative source -
    duplicating them here would be exactly the "unnecessary duplicate
    persistence" this project has avoided everywhere else (see
    `SessionRecord`'s own docstring on why it embeds a snapshot only where
    no other authoritative copy exists).
    """

    model_config = ConfigDict(frozen=False)

    evaluation_id: str
    session_id: str
    interview_id: str
    candidate_id: str
    job_id: str
    # Chunk 2 bridge - None for a session created without an application_id
    # (the original /sessions contract, still fully supported). See
    # services/evaluation_service.py for why evaluation cannot run without
    # one today: ScoringAgent requires a MatchingScore, and the only place
    # this backend has one is Application.matching_score.
    application_id: Optional[str] = None

    status: EvaluationStatus = EvaluationStatus.PENDING
    result: Optional[CandidateReport] = None

    # Set only on FAILED - the reason a required stage could not produce a
    # result (e.g. "technical evaluation failed: ..."). Never set on
    # COMPLETED - see `warnings` for that.
    error: Optional[str] = None
    # Non-fatal issues from an OPTIONAL stage (resume audit / integrity /
    # bias check) that failed but did not prevent a real CandidateReport
    # from being produced - Step 8's "do not throw away explainability
    # information" applied to the evaluation's own operational history, not
    # just the report's content.
    warnings: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class EvaluationRepository(ABC):
    """Durable storage for `EvaluationJob`.

    Five methods, each covering a distinct, actually-needed operation - no
    speculative surface:

      `save`                       status/result/failure updates (upsert).
      `get`                        direct lookup, once evaluation_id is known.
      `get_for_session`            the idempotency read - "does one already
                                    exist for this session".
      `create_if_absent_for_session`  the idempotency WRITE - the one
                                    operation that must be atomic, so two
                                    concurrent triggers for the same session
                                    (Chunk 4 Step 6: duplicate finish,
                                    network retry, submit-vs-finish racing)
                                    can never create two evaluation jobs
                                    for it. Mirrors
                                    `api.registry.SessionRegistry.restore_session`'s
                                    exact "existing wins" pattern for the
                                    identical class of race.
      `get_many_for_sessions`      Chunk 5's batch read - see its own
                                    docstring.
    """

    @abstractmethod
    async def save(self, job: EvaluationJob) -> EvaluationJob:
        """Insert or update by `evaluation_id`. Idempotent - saving the same
        COMPLETED job twice (e.g. a defensive re-save) must not create a
        duplicate or corrupt the stored result."""

    @abstractmethod
    async def get(self, evaluation_id: str) -> Optional[EvaluationJob]:
        """The job, or None. Never raises for absence."""

    @abstractmethod
    async def get_for_session(self, session_id: str) -> Optional[EvaluationJob]:
        """The evaluation job for this session, if one has ever been
        triggered - at most one ever exists, enforced by
        `create_if_absent_for_session`."""

    @abstractmethod
    async def create_if_absent_for_session(self, job: EvaluationJob) -> EvaluationJob:
        """Atomically insert `job` UNLESS one already exists for
        `job.session_id`, in which case the EXISTING job is returned instead
        and `job` is discarded.

        Callers distinguish "I created it" from "someone already did" by
        comparing `returned.evaluation_id == job.evaluation_id`: only the
        winner may schedule background execution - the loser's job was
        never stored and must not be treated as real.
        """

    @abstractmethod
    async def get_many_for_sessions(self, session_ids: list[str]) -> dict[str, EvaluationJob]:
        """The evaluation job for each of the given session_ids that has
        one, keyed by session_id - a session with no evaluation triggered
        yet is simply absent from the returned dict, never an error.

        Chunk 5: `RecruiterService` resolves evaluation status for a whole
        job's worth of applications in one call instead of one per
        application - the same N+1-avoidance reasoning as
        `SessionRepository.get_many`. Keyed by session_id (not
        evaluation_id) because that is the join key every caller actually
        has in hand (`Application.session_id`), not the evaluation_id,
        which is only known once an evaluation already exists.
        """


__all__ = [
    "Application",
    "ApplicationRepository",
    "CandidateRecord",
    "CandidateRepository",
    "EvaluationJob",
    "EvaluationRepository",
    "EvaluationStatus",
    "JobRecord",
    "JobRepository",
    "RepositoryError",
    "SessionRecord",
    "SessionRepository",
    "SessionRuntimeRegistry",
    "TranscriptRepository",
    "UserRecord",
    "UserRepository",
]
