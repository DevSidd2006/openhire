"""
Interview session use cases.

This service is the only thing that sits between the HTTP layer and
`InterviewSessionRunner`. It exists because the routes had grown a second
job: as well as translating HTTP, they were deciding *when* a session gets
recorded, *what* gets recorded, and how domain exceptions become status
codes. Those are application concerns, not transport concerns, and they are
about to multiply once the database and the evaluation trigger land.

The boundary it does NOT cross
------------------------------
No interview intelligence. This service never chooses a competency, never
phrases a question, never judges an answer, never builds evidence and never
computes a score. Every one of those decisions is made by
`InterviewSessionRunner` and the P3 engine behind it, and this service's
methods are each a single call into that runner plus bookkeeping around it.
If a method in this file ever needs to inspect `answer_text` or reason about
a `competency`, the logic has been put in the wrong layer.

Persistence failure policy
--------------------------
Two different writes happen here, and they are treated differently on
purpose.

The session *record* (`SessionRecord`) is derived, recomputable metadata -
at any moment it can be rebuilt from the live runner. A failure to write it
is logged with the `PERSISTENCE_FAILURE` marker and otherwise swallowed: a
candidate mid-interview cannot act on a storage hiccup, and failing their
request over recomputable bookkeeping would discard a real answer the
runner has already accepted.

The sealed *transcript* is different: it is the only record of the
interview's content, and a database is not involved yet to rebuild it from.
So a transcript write is never allowed to look like it succeeded when it
did not:

  * `_persist_transcript` reports True/False - it does not swallow the
    outcome the way the record write does.
  * `get_transcript_persistence_status` / `retry_transcript_persistence`
    answer "is it actually in the repository right now" by asking the
    repository directly, never a cached flag - correct even though a fresh
    `InterviewService` is constructed for every request (see
    core/dependencies.py), because `self._transcripts` is the one long-lived
    object shared across requests via the container.
  * Callers (api/routes/interview.py) surface the outcome to the client as
    an explicit `transcript_persisted` field rather than reporting bare
    success. This is deliberately NOT a 5xx on the completing request: the
    interview itself genuinely finished (P4's engine rules are untouched),
    and a candidate cannot fix a storage fault - so failing their last
    answer/finish call would tell them their interview failed when it did
    not. The failure is instead visible in the response body and retried
    the next time the session is read.
  * Retrying is safe to call any number of times: `TranscriptRepository.save`
    upserts by `interview_id` (repositories/memory.py), so re-persisting an
    already-stored transcript is a no-op in effect, and
    `retry_transcript_persistence` additionally short-circuits without a
    write at all once the repository already has it.

What this does NOT add: a durable outbox, a scheduled retry worker, or a new
endpoint. Retries happen lazily, as a side effect of a client re-reading
session state (`GET /sessions/{id}`) after a failure - which is the natural
place a client already looks to find out what happened. A background
worker that retries without being asked is a later chunk's job, once it
also owns the database write path.

Session restoration (Chunk 3)
------------------------------
`get_runner` no longer only checks the runtime registry. If a session_id is
not (or no longer) resident there - the process restarted, or this is a
different worker than the one that created it - it falls back to
`_restore_runner`, which rebuilds a live `InterviewSessionRunner` purely
from its own persisted `SessionRecord` (see that model's docstring in
repositories/interfaces.py) via
`InterviewSessionRunner.rehydrate`, then registers it under its ORIGINAL
session_id via `SessionRegistry.restore_session` (distinct from
`create_session`, which always mints a new one) so every subsequent call in
this process hits the fast, already-in-memory path.

This is entirely transparent to every caller: `submit_answer`,
`finish_session`, the voice transport, and every route in
api/routes/interview.py and api/routes/voice.py already go through
`get_runner`, so none of them needed to change for restoration to work.

A second, related gap this chunk closes: previously, if `runner.submit_answer`
or `runner.request_finish` raised (the runner moving to FAILED - an
evaluation or question-generation failure that exhausted its retry budget),
the exception propagated straight out of this service WITHOUT ever calling
`_persist_record` - so that FAILED transition was silently never durably
recorded, and a restored copy of that session would incorrectly still look
ACTIVE. Both methods now persist the record on that exception path too,
before re-raising it unchanged.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from api.exceptions import SessionNotFoundError

if TYPE_CHECKING:
    from api.registry import SessionRegistry
from core.errors import NotFoundError
from core.logging import get_logger, log_context
from repositories.interfaces import (
    SessionRecord,
    SessionRepository,
    TranscriptRepository,
)
from schemas.interview import InterviewQuestion, InterviewState
from schemas.job import JobDescription
from schemas.resume import ParsedResume
from utils.interview_session import (
    AnswerSubmissionResult,
    InterviewSessionError,
    InterviewSessionRunner,
    SessionStatus,
)

logger = get_logger("services.interview")


class InterviewService:
    """Use cases for one interview session.

    Constructed per request (it is cheap - it holds references, not state)
    from the container plus the app's runtime registry. Holding no mutable
    state of its own is what makes it safe under concurrency; all session
    state lives in the runner, behind the runner's own lock, and all
    persistence state lives in the repositories, which are long-lived
    objects shared across requests via the container - never in this
    object.
    """

    def __init__(
        self,
        *,
        registry: SessionRegistry,
        session_repository: SessionRepository,
        transcript_repository: TranscriptRepository,
        interviewer_factory=None,
    ) -> None:
        self._registry = registry
        self._sessions = session_repository
        self._transcripts = transcript_repository
        # The existing `app.state.interviewer_factory` wiring seam, passed
        # through unchanged. None means "let InterviewSessionRunner resolve
        # its own default via get_llm_provider()", which is what a real
        # deployment always does.
        self._interviewer_factory = interviewer_factory

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def create_session(
        self,
        *,
        job_description: JobDescription,
        parsed_resume: ParsedResume,
        candidate_id: str,
        max_questions: Optional[int] = None,
        application_id: Optional[str] = None,
    ) -> tuple[str, InterviewSessionRunner]:
        """Create a session, ask the first question, and record it.

        Ordering is deliberate: `start()` runs before the session is
        registered or recorded. A session that failed to produce its first
        question is not a session, and registering one would leave a dead
        entry addressable by session_id.

        `application_id` (Chunk 2's bridge, api/routes/interview.py) is
        stored on the resulting `SessionRecord` exactly once, here - it
        never changes for the lifetime of the session, so no later
        `_persist_record` call needs to know about it (see that method for
        how it is carried forward unchanged on every subsequent save).
        """
        runner = InterviewSessionRunner(
            job_description,
            parsed_resume,
            candidate_id=candidate_id,
            max_questions=max_questions,
            interviewer=self._interviewer_factory() if self._interviewer_factory else None,
        )
        await runner.start()

        session_id = await self._registry.create_session(runner)
        await self._persist_record(session_id, runner, application_id=application_id)

        logger.info(
            "session created",
            extra=log_context(
                event="session_created",
                session_id=session_id,
                candidate_id=candidate_id,
                job_id=job_description.job_id,
            ),
        )
        question = runner.get_current_question()
        if question is not None:
            self._log_question(session_id, question)
        return session_id, runner

    async def get_runner(self, session_id: str) -> InterviewSessionRunner:
        """Resolve a live session by its opaque id - restoring it from
        persisted state (Chunk 3) if it is not currently resident in the
        runtime registry.

        The id is the ONLY lookup key, exactly as api/registry.py
        established - candidate_id and job_id are never accepted here, so
        one candidate's identifier can never reach another's session.

        `SessionNotFoundError` is re-raised untouched (not converted) for a
        session_id that cannot be resolved even via restoration: its
        dedicated 404 handler and the tests built on it predate this
        service, and swallowing it into a generic `NotFoundError` would
        change a response shape for no benefit. This applies uniformly
        whether the id genuinely never existed or a persisted record exists
        but cannot be rehydrated (e.g. it predates Chunk 3 and has no
        `state` at all) - from the client's perspective both are simply
        "this session cannot be retrieved", and collapsing them keeps the
        API contract exactly what it already was.
        """
        try:
            return await self._registry.get_session(session_id)
        except SessionNotFoundError:
            pass

        runner = await self._restore_runner(session_id)
        return await self._registry.restore_session(session_id, runner)

    async def submit_answer(self, session_id: str, answer_text: str) -> AnswerSubmissionResult:
        """Submit one answer.

        The answer text is untrusted candidate content and is passed
        straight through to the runner, which is the only component that
        evaluates it. This method never inspects, trims, normalises or
        interprets it - doing so would silently change what the interview
        engine sees.

        If this turn seals the session, the sealed transcript is persisted
        before returning; the outcome (True/False) is discoverable
        afterwards via `get_transcript_persistence_status(runner)` - the
        route handler attaches it to the response as `transcript_persisted`
        rather than this method changing its own return type or raising for
        a storage fault the candidate cannot act on.

        If the runner instead raises (moving to FAILED - an evaluation
        failure that exhausted its retry budget), that transition is still
        persisted before the exception is re-raised unchanged, so it is not
        silently lost - see the module docstring.
        """
        runner = await self.get_runner(session_id)
        try:
            result = await runner.submit_answer(answer_text)
        except InterviewSessionError:
            await self._persist_record(session_id, runner)
            raise

        logger.info(
            "answer submitted",
            extra=log_context(
                event="answer_submitted",
                session_id=session_id,
                question_id=result.question.question_id,
                competency=result.question.competency,
                # Length only. The answer itself is candidate data and is
                # never written to a log.
                answer_length=len(answer_text),
            ),
        )

        await self._persist_record(session_id, runner)
        if result.session_status == SessionStatus.SEALED:
            await self._on_sealed(session_id, runner, result.termination_reason)
        elif result.next_question is not None:
            self._log_question(session_id, result.next_question)

        return result

    async def finish_session(self, session_id: str) -> InterviewState:
        """Request explicit termination.

        Delegates entirely to `request_finish()`, which routes through the
        same decide/validate/seal path as any other termination - this
        service cannot and must not bypass the engine's termination rules.
        See `submit_answer` above for how the resulting transcript
        persistence outcome is surfaced, and for why a FAILED transition is
        also persisted on the exception path below.
        """
        runner = await self.get_runner(session_id)
        try:
            state = await runner.request_finish()
        except InterviewSessionError:
            await self._persist_record(session_id, runner)
            raise

        await self._persist_record(session_id, runner)
        if runner.status == SessionStatus.SEALED:
            await self._on_sealed(session_id, runner, state.termination_reason)
        return state

    async def record_turn_outcome(
        self, session_id: str, runner: InterviewSessionRunner
    ) -> None:
        """Record a session whose turn was driven by something other than
        this service.

        The voice layer (utils/voice_turn.py) calls
        `InterviewSessionRunner.submit_answer()` itself - it orchestrates
        STT, the engine turn and TTS as one unit, and routing it through
        `submit_answer` above would mean either duplicating that
        orchestration here or rewriting the voice layer, neither of which is
        this chunk's business. So the voice transport calls this instead,
        after its turn completes, and a spoken interview gets exactly the
        same durable record and transcript as a typed one, including the
        same discoverable persistence outcome via
        `get_transcript_persistence_status`.

        Idempotent and read-only with respect to the interview: it projects
        the runner's current state and writes it. Calling it twice for the
        same turn stores the same record and re-persists the same
        transcript (a safe no-op - see the module docstring).
        """
        await self._persist_record(session_id, runner)
        if runner.status == SessionStatus.SEALED:
            await self._persist_transcript(session_id, runner)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_record(self, session_id: str) -> SessionRecord:
        """The durable record for a session.

        Reads through the persistence boundary rather than the runtime
        registry, so it will keep working across a restart once a real
        repository is installed. Raises `NotFoundError` (not
        `SessionNotFoundError`) because absence here means "no stored
        record", which is a different fact from "no live session".
        """
        record = await self._sessions.get(session_id)
        if record is None:
            raise NotFoundError(
                "No session found for the given session_id",
                internal_detail=f"session record missing for session_id={session_id!r}",
            )
        return record

    async def get_transcript_persistence_status(
        self, runner: InterviewSessionRunner
    ) -> Optional[bool]:
        """Whether this session's sealed transcript is durably stored, right
        now.

        Returns `None` if the session has not sealed - there is nothing to
        persist yet, and that is not a failure. Otherwise returns whether
        the transcript is actually present in `TranscriptRepository`, checked
        live rather than from a cached flag: this object is rebuilt fresh
        for every request (see core/dependencies.py), so nothing it holds in
        memory could survive between calls - only `self._transcripts`, the
        long-lived repository handed in by the container, can answer this
        correctly across requests.
        """
        if runner.status != SessionStatus.SEALED:
            return None
        stored = await self._transcripts.get(runner.interview_id)
        return stored is not None

    async def retry_transcript_persistence(
        self, session_id: str, runner: InterviewSessionRunner
    ) -> Optional[bool]:
        """Retry storing a sealed transcript that previously failed to save.

        Called from the session-state read path (`GET /sessions/{id}`) so
        that a client checking on their session after a storage failure is
        also the trigger that retries it - no separate worker or schedule
        required for Chunk 1.

        Safe to call unconditionally on every read of a sealed session:
        if the transcript is already stored this makes no write at all, and
        if it previously failed, retrying calls the exact same
        `_persist_transcript` path a client's next answer/finish call would
        have used - so a client that simply retries their last action
        instead of polling gets the identical, idempotent behaviour.
        """
        if runner.status != SessionStatus.SEALED:
            return None
        if await self.get_transcript_persistence_status(runner):
            return True
        return await self._persist_transcript(session_id, runner)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _on_sealed(
        self, session_id: str, runner: InterviewSessionRunner, termination_reason: Optional[str]
    ) -> None:
        logger.info(
            "interview finished",
            extra=log_context(
                event="interview_finished",
                session_id=session_id,
                termination_reason=termination_reason,
            ),
        )
        await self._persist_transcript(session_id, runner)

    def _log_question(self, session_id: str, question: InterviewQuestion) -> None:
        """Log that a question was presented - id and competency only.

        `question.reason` is never logged: it is the adaptive engine's own
        internal justification ("SQL evidence is insufficient, confidence
        0.32") and api/models.py already withholds it from the candidate.
        """
        logger.info(
            "question presented",
            extra=log_context(
                event="question_presented",
                session_id=session_id,
                question_id=question.question_id,
                competency=question.competency,
            ),
        )

    async def _persist_record(
        self,
        session_id: str,
        runner: InterviewSessionRunner,
        *,
        application_id: Optional[str] = None,
    ) -> None:
        """Best-effort write of the session's durable (and, as of Chunk 3,
        restorable) record.

        Non-fatal by design: unlike the interview STATE it now embeds, this
        write itself is still recomputable from the live runner at any
        time (that is exactly what `SessionRecord.from_runner` does), so a
        storage failure must not fail a live interview request the engine
        has already completed. This is deliberately different from
        `_persist_transcript` below - see the module docstring for why the
        transcript does not get the same treatment.

        `application_id` carries forward from the previously-stored record
        when not explicitly given: `SessionRecord.from_runner` always builds
        a FRESH record from the runner alone, which has no notion of
        "application" at all, so every caller except `create_session` (the
        only place an application is ever linked) must not accidentally
        reset it back to None on every subsequent turn.
        """
        try:
            existing = await self._sessions.get(session_id)
            record = SessionRecord.from_runner(session_id, runner)
            carried_application_id = (
                application_id if application_id is not None
                else (existing.application_id if existing is not None else None)
            )
            record = record.model_copy(update={"application_id": carried_application_id})
            await self._sessions.save(record)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "PERSISTENCE_FAILURE session record not saved: %s",
                exc,
                exc_info=True,
                extra=log_context(event="persistence_failure", kind="session_record",
                                  session_id=session_id),
            )

    async def _restore_runner(self, session_id: str) -> InterviewSessionRunner:
        """Reconstruct a live runner purely from ITS OWN persisted
        `SessionRecord` (Chunk 3).

        Never re-fetches `job_description`/`parsed_resume` from a
        job/candidate repository: `SessionRecord` already carries the
        runner's own original private copies (see that model's docstring in
        repositories/interfaces.py) - which is what makes restoration immune
        to a job being edited or archived, or in principle a candidate
        record changing, after this interview began. Accordingly this
        service has no dependency on `JobRepository`/`CandidateRepository`
        at all.

        Raises `SessionNotFoundError` (not a different error type) whenever
        restoration is not possible, for any reason - no persisted record,
        or a record predating Chunk 3 with no `state`/`job_description`/
        `parsed_resume`. See `get_runner`'s docstring for why that is the
        right client-facing behavior.
        """
        record = await self._sessions.get(session_id)
        if (
            record is None
            or record.state is None
            or record.job_description is None
            or record.parsed_resume is None
            or record.interview_id is None
        ):
            raise SessionNotFoundError(session_id)

        try:
            runner = InterviewSessionRunner.rehydrate(
                job_description=record.job_description,
                parsed_resume=record.parsed_resume,
                state=record.state,
                status=record.status,
                candidate_id=record.candidate_id,
                interview_id=record.interview_id,
                interviewer=self._interviewer_factory() if self._interviewer_factory else None,
            )
        except InterviewSessionError as exc:
            # The persisted state exists but is not internally consistent
            # (a corrupted/foreign record) - functionally as unusable as no
            # record at all, so it gets the same client-facing outcome.
            logger.error(
                "PERSISTENCE_FAILURE session could not be restored: %s",
                exc,
                exc_info=True,
                extra=log_context(event="restoration_failed", session_id=session_id),
            )
            raise SessionNotFoundError(session_id) from exc

        logger.info(
            "session restored from persisted state",
            extra=log_context(
                event="session_restored", session_id=session_id, status=record.status.value,
            ),
        )
        return runner

    async def _persist_transcript(self, session_id: str, runner: InterviewSessionRunner) -> bool:
        """Attempt to store the sealed transcript. Returns whether it is now
        actually present in the repository.

        Unlike `_persist_record`, this never pretends a failure is a
        success: it returns False rather than swallowing the exception, so
        every caller (`_on_sealed`, `record_turn_outcome`,
        `retry_transcript_persistence`) can see the true outcome and the
        route layer can report it to the client as
        `transcript_persisted: false` instead of a bare `status: "sealed"`
        that looks identical whether the write worked or not.

        Still does not raise: the interview genuinely completed (the
        candidate answered every question, or explicitly asked to finish),
        and there is nothing they can do about a storage fault, so turning
        this into a 5xx on their last request would tell them their
        interview failed when it did not. The failure is logged with the
        `PERSISTENCE_FAILURE` marker for alerting, and remains retryable via
        `retry_transcript_persistence` for as long as the runner stays in
        the registry.
        """
        try:
            transcript = runner.get_transcript()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "PERSISTENCE_FAILURE sealed transcript unavailable: %s",
                exc,
                exc_info=True,
                extra=log_context(event="persistence_failure", kind="transcript",
                                  session_id=session_id),
            )
            return False

        try:
            await self._transcripts.save(transcript)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "PERSISTENCE_FAILURE sealed transcript not saved (interview_id=%s): %s",
                transcript.interview_id,
                exc,
                exc_info=True,
                extra=log_context(event="persistence_failure", kind="transcript",
                                  session_id=session_id),
            )
            return False

        logger.info(
            "transcript stored",
            extra=log_context(
                event="transcript_stored",
                session_id=session_id,
                interview_id=transcript.interview_id,
                exchanges=len(transcript.exchanges),
            ),
        )
        return True


__all__ = ["InterviewService"]
