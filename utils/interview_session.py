"""
P4: Turn-based interview session runner.

P3 built the adaptive engine (utils/adaptive_interview.py: pure decision
logic) and gave InterviewerAgent two LLM-calling methods (evaluate_answer,
generate_next_question) but deliberately did not wire them into an
executable, stateful session - see utils/adaptive_interview.py's module
docstring and agents/interviewer/agent.py's P3 section. This module is that
missing layer.

Strict separation of responsibilities (unchanged by P4, just finally wired
together):
    InterviewSessionRunner (this file) - lifecycle + sequencing ONLY. Never
        computes a competency priority, an action, or a score.
    utils/adaptive_interview.py        - decision + prioritization + state
        transitions. Never calls an LLM.
    agents/interviewer/agent.py        - LLM question phrasing / answer
        judgment. Never decides what happens next.
    utils/evidence.py                  - canonical evidence construction
        (via utils.adaptive_interview.build_answer_evidence).

The runner produces a sealed InterviewTranscript - the exact same model the
pre-P3 pipeline already consumes (schemas/interview.py:InterviewTranscript,
orchestration/graph.py's `interview_transcripts` PipelineState input). It
does not introduce a second transcript format, evidence format, or scoring
path; see tests/test_interview_session.py for a sealed transcript produced
here flowing into the real, unmodified evaluation/scoring/report/leaderboard
pipeline.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional

from pydantic import BaseModel, ConfigDict

from agents.interviewer.agent import InterviewerAgent
from schemas.evaluation import EvidenceItem
from schemas.interview import (
    InterviewAnswer,
    InterviewQuestion,
    InterviewState,
    InterviewTranscript,
    NextQuestionDecision,
)
from schemas.job import JobDescription
from schemas.resume import ParsedResume
from utils.adaptive_interview import (
    AdaptiveInterviewError,
    DuplicateQuestionError,
    apply_finish,
    build_answer_evidence,
    decide_next_action,
    record_answer,
    start_question,
    validate_decision,
)

# Bounded regeneration budget for a duplicate question (P4 Phase 19): P3's
# generate_next_question raises DuplicateQuestionError rather than silently
# reusing/replacing a duplicate. A fresh LLM call with the exact same
# decision context can plausibly phrase a different question, so a small,
# FINITE number of re-attempts is worth it before giving up - this must
# never become an unbounded loop.
MAX_QUESTION_REGENERATION_ATTEMPTS = 3


class SessionStatus(str, Enum):
    """Lifecycle of one InterviewSessionRunner (P4 Phase 3). No existing
    enum in the codebase represents this - InterviewState only tracks
    `is_completed`/`termination_reason` (P3), which is a narrower concept
    (interview logic done) than session lifecycle (has start() even been
    called yet? did evaluation fail outright?)."""
    CREATED = "created"
    ACTIVE = "active"
    FINISHING = "finishing"
    SEALED = "sealed"
    FAILED = "failed"


class InterviewSessionError(Exception):
    """An operation was attempted that the session's current lifecycle
    state does not allow, or the session could not proceed at all
    (evaluation/generation failure exhausted the existing provider retry
    budget). Always raised explicitly - the session is never left silently
    inconsistent, and no fabricated question/score/evidence is ever
    substituted in its place (P0/P4 Phase 10)."""


class AnswerSubmissionResult(BaseModel):
    """What submit_answer() returns - only what a caller actually needs
    (P4 Phase 5), not the whole transcript. `question`/`answer` are the
    exact pair just processed (never re-derived or paraphrased);
    `next_question` is None exactly when the session has finished."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    question: InterviewQuestion
    answer: InterviewAnswer
    evidence: EvidenceItem
    next_question: Optional[InterviewQuestion] = None
    session_status: SessionStatus
    termination_reason: Optional[str] = None


class InterviewSessionRunner:
    """Owns the lifecycle of ONE candidate's adaptive interview session.

    Pure application-layer component: no web framework, no HTTP, no
    websocket dependency. A caller drives it with a plain loop:

        runner = InterviewSessionRunner(job_description, parsed_resume)
        question = await runner.start()
        while question is not None:
            result = await runner.submit_answer(candidate_answer_text)
            question = result.next_question
        transcript = runner.get_transcript()  # sealed, ready for the
                                               # existing evaluation pipeline
    """

    def __init__(
        self,
        job_description: JobDescription,
        parsed_resume: ParsedResume,
        *,
        candidate_id: Optional[str] = None,
        interviewer: Optional[InterviewerAgent] = None,
        interview_id: Optional[str] = None,
        max_questions: Optional[int] = None,
    ):
        self.job_description = job_description
        self.parsed_resume = parsed_resume
        self.interviewer = interviewer or InterviewerAgent()
        self._candidate_id = candidate_id or parsed_resume.candidate_id
        self._interview_id = interview_id or f"int_{uuid.uuid4().hex[:8]}"
        self._max_questions = max_questions

        self.status: SessionStatus = SessionStatus.CREATED
        self._state: Optional[InterviewState] = None
        self._transcript: Optional[InterviewTranscript] = None
        self._previous_answer: Optional[InterviewAnswer] = None

        # P4 Phase 12: serializes submit_answer() so two concurrent calls
        # for the same pending question can never both apply a state
        # transition. Application-level only - no distributed locking.
        self._lock = asyncio.Lock()

        # P4 Phase 11: idempotency cache, keyed by the question_id a
        # submission answered. A second submit_answer() call that reaches
        # the lock after the first has already processed THAT SAME pending
        # question returns the original result instead of being applied to
        # whatever question is current now.
        self._last_submission_by_question: Dict[str, AnswerSubmissionResult] = {}

        # P4 Phase 10: if answer evaluation fails outright, the raw answer
        # is preserved here (never lost) even though it could not be
        # recorded into InterviewState (recording requires a real
        # evaluation - P3's record_answer never accepts a fabricated one).
        self.last_failed_answer: Optional[InterviewAnswer] = None

    # ---------------------------------------------------------------
    # Read-only accessors
    # ---------------------------------------------------------------

    def get_state(self) -> InterviewState:
        if self._state is None:
            raise InterviewSessionError("Session has not been started")
        return self._state

    def get_current_question(self) -> Optional[InterviewQuestion]:
        return self.get_state().current_question

    def is_finished(self) -> bool:
        """True once no further interview turns will happen - either the
        interview completed and sealed normally, or the session failed."""
        return self.status in (SessionStatus.SEALED, SessionStatus.FAILED)

    def get_transcript(self) -> InterviewTranscript:
        """Only available once SEALED. Returns a deep copy so a caller
        mutating the returned object can never affect the runner's own
        record - the existing InterviewTranscript model has no frozen/
        immutability config of its own (P2 did not add one), so this is a
        defensive copy on top of the existing model, not a new sealing
        mechanism."""
        if self._transcript is None:
            raise InterviewSessionError("Transcript is only available after the session is sealed")
        return self._transcript.model_copy(deep=True)

    # ---------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------

    async def start(self) -> Optional[InterviewQuestion]:
        """CREATED -> ACTIVE, initialize InterviewState, and determine the
        first question via the P3 decision engine (P4 Phase 4)."""
        if self.status != SessionStatus.CREATED:
            raise InterviewSessionError(f"Cannot start a session in status {self.status.value}")

        now = datetime.now(timezone.utc).isoformat()
        state = InterviewState(
            interview_id=self._interview_id,
            candidate_id=self._candidate_id,
            job_id=self.job_description.job_id,
            start_time=now,
            last_activity_time=now,
        )
        if self._max_questions is not None:
            state = state.model_copy(update={"max_questions": self._max_questions})
        self._state = state
        self.status = SessionStatus.ACTIVE

        return await self._advance()

    async def submit_answer(self, answer_text: str) -> AnswerSubmissionResult:
        """The central operation (P4 Phase 5). See module docstring for the
        full flow; P4 Phase 6/7 (answer/question immutability), Phase 10
        (failure safety), Phase 11 (idempotency) and Phase 12 (concurrency)
        are all enforced here."""
        if self.status != SessionStatus.ACTIVE:
            raise InterviewSessionError(f"Cannot submit an answer while session is {self.status.value}")

        pending = self.get_state().current_question
        if pending is None:
            raise InterviewSessionError("No question is currently pending an answer")
        pending_id = pending.question_id

        async with self._lock:
            # Re-check AFTER acquiring the lock: a concurrent submit_answer()
            # call may have already completed this exact turn while this
            # call was waiting (P4 Phase 11/12) - return its result rather
            # than process a second, duplicate answer for the same question.
            cached = self._last_submission_by_question.get(pending_id)
            if cached is not None:
                return cached
            if self.status != SessionStatus.ACTIVE:
                raise InterviewSessionError(f"Cannot submit an answer while session is {self.status.value}")
            current = self.get_state().current_question
            if current is None or current.question_id != pending_id:
                raise InterviewSessionError(
                    f"Question {pending_id!r} is no longer pending - cannot submit an answer for it"
                )

            # Question immutability (P4 Phase 7): `question` below is the
            # exact InterviewQuestion object already in state - never
            # regenerated or altered here.
            question = current
            # Answer immutability (P4 Phase 6): the candidate's text is
            # captured verbatim into InterviewAnswer.answer_text and never
            # rewritten afterward; if any normalization is ever needed for
            # analysis it must be stored separately, never in this field.
            answer = InterviewAnswer(question_id=question.question_id, answer_text=answer_text)
            self.last_failed_answer = None

            try:
                evaluation = await self.interviewer.evaluate_answer(
                    question, answer, question.competency or "general"
                )
            except Exception as exc:
                # Failure safety (P4 Phase 10): the answer is preserved
                # (last_failed_answer) even though it cannot be committed to
                # InterviewState - P3's record_answer() only ever accepts a
                # real AnswerEvaluationResult, never a fabricated one, and
                # the provider-level retry budget (BaseAgent._call_with_retry,
                # P0-6) has already been exhausted by the time this
                # exception reaches us. current_question stays pending
                # (unchanged), so the session state is never left partially
                # updated; it moves to FAILED rather than silently skipping
                # the turn or continuing with a guessed evaluation.
                self.last_failed_answer = answer
                self.status = SessionStatus.FAILED
                raise InterviewSessionError(f"Answer evaluation failed: {exc}") from exc

            competency = question.competency or "general"
            evidence = build_answer_evidence(self.get_state(), question, answer, evaluation, competency)
            state = record_answer(self.get_state(), answer, evaluation, competency)
            state = state.model_copy(update={"last_activity_time": datetime.now(timezone.utc).isoformat()})
            self._state = state
            self._previous_answer = answer

            next_question = await self._advance()

            result = AnswerSubmissionResult(
                question=question,
                answer=answer,
                evidence=evidence,
                next_question=next_question,
                session_status=self.status,
                termination_reason=self._state.termination_reason if self._state else None,
            )
            self._last_submission_by_question[pending_id] = result
            return result

    async def request_finish(self) -> InterviewState:
        """P5: explicit termination requested by a caller (e.g. the HTTP
        POST /sessions/{id}/finish endpoint), extending the runner minimally
        rather than adding a second finish/sealing path. Routes through the
        exact same decide -> validate -> apply_finish -> _seal sequence as
        every other termination (_advance(), above) - it cannot fake a
        reason like "sufficient_evidence_collected" that isn't true, because
        validate_decision() only ever accepts a forced reason when it is
        literally "explicit_termination" (see utils/adaptive_interview.py).
        Any question still pending and unanswered is simply not part of the
        sealed transcript (the transcript only ever contains answered
        exchanges) - this is the same semantics as any other termination
        that happens to land on a question the candidate had not answered.
        """
        if self.status != SessionStatus.ACTIVE:
            raise InterviewSessionError(f"Cannot finish a session in status {self.status.value}")
        async with self._lock:
            if self.status != SessionStatus.ACTIVE:
                raise InterviewSessionError(f"Cannot finish a session in status {self.status.value}")
            await self._advance(force_reason="explicit_termination")
            return self.get_state()

    async def _advance(self, *, force_reason: Optional[str] = None) -> Optional[InterviewQuestion]:
        """The single place decide_next_action() is invoked - called once
        from start(), once after every recorded answer, and once from
        request_finish() (P5), so P3's decide -> generate/finish loop has
        exactly one implementation (P4 Phase 9: "the runner should
        orchestrate; P3 contains the decision logic - do not duplicate
        it"). `force_reason` is threaded straight through to P3's own
        decide_next_action(force_reason=...) - added (P5) only so
        request_finish() can ask for the SAME forced-termination path P3
        already validates (see validate_decision's "explicit_termination"
        carve-out), not a second termination mechanism."""
        state = self.get_state()
        try:
            decision = decide_next_action(state, self.job_description, force_reason=force_reason)
            validate_decision(decision, state, self.job_description)
        except AdaptiveInterviewError as exc:
            self.status = SessionStatus.FAILED
            raise InterviewSessionError(f"Invalid interview state transition: {exc}") from exc

        if decision.action == "finish":
            self._state = apply_finish(state, decision)
            self.status = SessionStatus.FINISHING
            self._seal()
            return None

        question = await self._generate_question_with_retry(decision)
        self._state = start_question(state, question, decision)
        return question

    async def _generate_question_with_retry(self, decision: NextQuestionDecision) -> InterviewQuestion:
        """P4 Phase 19: P3's generate_next_question raises
        DuplicateQuestionError instead of silently substituting a repeated
        question. A bounded number of fresh LLM calls (same decision
        context - the target competency/action never changes) get a chance
        to phrase something new; exhausting the budget fails the session
        explicitly rather than looping forever or fabricating a fallback
        question (P4 Phase 7/10)."""
        last_error: Optional[Exception] = None
        for _attempt in range(1, MAX_QUESTION_REGENERATION_ATTEMPTS + 1):
            try:
                return await self.interviewer.generate_next_question(
                    self.job_description, self.parsed_resume, self.get_state(), decision,
                    previous_answer=self._previous_answer,
                )
            except DuplicateQuestionError as exc:
                last_error = exc
                continue
            except Exception as exc:
                self.status = SessionStatus.FAILED
                raise InterviewSessionError(f"Question generation failed: {exc}") from exc

        self.status = SessionStatus.FAILED
        raise InterviewSessionError(
            f"Question generation kept producing duplicates after "
            f"{MAX_QUESTION_REGENERATION_ATTEMPTS} attempts: {last_error}"
        )

    def _seal(self) -> None:
        """FINISHING -> SEALED (P4 Phase 13). Validates the transcript's
        structural invariants before sealing - uses the EXISTING
        InterviewTranscript model and its existing is_sealed/seal_timestamp
        fields (same pattern as main.py/tests/conftest.py), never a second
        sealing mechanism."""
        state = self.get_state()
        if state.termination_reason is None:
            self.status = SessionStatus.FAILED
            raise InterviewSessionError("Cannot seal a transcript with no termination_reason")

        question_ids = [q.question_id for q, _ in state.exchanges]
        answer_ids = [a.question_id for _, a in state.exchanges]
        if len(question_ids) != len(set(question_ids)):
            self.status = SessionStatus.FAILED
            raise InterviewSessionError("Cannot seal: duplicate question IDs in transcript")
        if len(answer_ids) != len(set(answer_ids)):
            self.status = SessionStatus.FAILED
            raise InterviewSessionError("Cannot seal: duplicate answer IDs in transcript")
        if any(q.question_id != a.question_id for q, a in state.exchanges):
            self.status = SessionStatus.FAILED
            raise InterviewSessionError("Cannot seal: an exchange's answer does not reference its own question")

        now = datetime.now(timezone.utc).isoformat()
        transcript = InterviewTranscript(
            interview_id=self._interview_id,
            candidate_id=self._candidate_id,
            job_id=self.job_description.job_id,
            start_time=state.start_time,
            end_time=now,
            exchanges=list(state.exchanges),
            interview_type="adaptive",
            format="text",
            is_sealed=True,
            seal_timestamp=now,
        )
        self._transcript = transcript
        self.status = SessionStatus.SEALED
