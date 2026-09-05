"""
Evaluation use cases: Sealed Interview -> Evaluation Job -> the EXISTING
evaluation agents -> Final Evaluation -> persisted, retrievable result.

Architectural note (read before changing anything here)
---------------------------------------------------------
The obvious "reuse existing code" move would be to call
`orchestration.graph.get_pipeline().ainvoke(...)`. That pipeline is real,
unmodified, and NOT reimplemented here - but it is a batch, multi-candidate,
from-raw-text pipeline: its first three nodes unconditionally re-run
`JDAnalyzerAgent`/`ResumeParserAgent`/`ResumeMatcherAgent` on raw text with
no way to skip them, and its last node produces one shared leaderboard
across every candidate. Invoking it per single sealed interview would (a)
waste duplicate LLM calls for a job/resume Chunk 2 already parsed, (b) risk
scoring against a job/resume RE-DERIVED by the pipeline that can diverge
from the exact snapshot the interview was conducted against (Chunk 3's
whole reason for existing - see repositories/interfaces.py:SessionRecord's
docstring), and (c) require every other candidate's report to exist just to
produce one candidate's evaluation.

So this service calls the SAME seven downstream agents
(`orchestration/graph.py`'s `node_run_parallel_evaluations` /
`node_run_bias_check` / `node_score_candidates` / `node_generate_reports`)
directly, in the exact same call contract, sequencing and concurrency
grouping those nodes already use, scoped to the one candidate whose
interview just sealed. This is not a second evaluation system - it is the
same agents, the same aggregation (`ScoringAgent`, `ReportGeneratorAgent`,
both deterministic and unmodified), reached through the entry point that
actually fits a single already-matched, already-interviewed candidate
instead of the batch entry point that doesn't.

Input policy: snapshots, never re-fetched
------------------------------------------
`job_description`/`parsed_resume` come from `SessionRecord` (the runner's
OWN copies, per Chunk 3) - never from `JobRepository`/`CandidateRepository`,
for the identical reason Chunk 3 rehydrates a runner from that same
snapshot: a job edited or a candidate profile changed after the interview
began must never retroactively change what is being evaluated.
`matching_score` comes from the linked `Application` (Chunk 2) - the same
`MatchingScore` that gated shortlisting, reused rather than recomputed
(`ScoringAgent` requires one; nothing in this chunk invents a new matching
algorithm to produce it another way). The sealed transcript comes from
`TranscriptRepository` (Chunk 1) - the authoritative copy; this service
never stores a second one.

Consequence, stated plainly: evaluation can only run for a session with an
`application_id` (Chunk 2's bridge). A session created via the original,
still-fully-supported `/sessions` contract with no `application_id` has no
`MatchingScore` anywhere in this backend to score it with, and
`trigger_evaluation` refuses with a clear reason rather than fabricating
one or silently skipping `ScoringAgent`'s required input.

Failure policy (Step 10)
--------------------------
Technical and behavioral evaluation are REQUIRED - `ScoringAgent` cannot run
without both (matching `orchestration/graph.py:node_score_candidates`'s own
exclusion rule, generalized to one candidate: no matching evaluation, no
score, ever). Either failing marks the WHOLE evaluation FAILED - never a
partial/fabricated score.

Resume audit, integrity and bias check are OPTIONAL - exactly as
`ReportGeneratorAgent`'s own signature already treats them (each defaults
gracefully to "no data" if not supplied). Any of them failing is recorded
as a non-fatal `warning` on the job; the evaluation still COMPLETES with a
real report, exactly as `node_generate_reports` already tolerates a missing
bias_audit/integrity_evaluation/resume_audits for any candidate.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from agents.behavioral_evaluator.agent import BehavioralEvaluatorAgent
from agents.bias_checker.agent import BiasCheckerAgent
from agents.integrity.agent import IntegrityAgent
from agents.report_generator.agent import ReportGeneratorAgent
from agents.resume_auditor.agent import ResumeAuditorAgent
from agents.scoring.agent import ScoringAgent
from agents.technical_evaluator.agent import TechnicalEvaluatorAgent
from core.errors import ConflictError, NotFoundError
from core.logging import get_logger, log_context
from orchestration.graph import _build_evaluation_sections
from repositories.interfaces import (
    ApplicationRepository,
    EvaluationJob,
    EvaluationRepository,
    EvaluationStatus,
    SessionRepository,
    TranscriptRepository,
)
from schemas.evaluation import (
    BehavioralEvaluation,
    BiasAudit,
    ClaimVerification,
    IntegrityEvaluation,
    MatchingScore,
    TechnicalEvaluation,
)
from schemas.interview import InterviewTranscript
from schemas.job import JobDescription
from schemas.resume import ParsedResume
from schemas.scoring import CandidateReport
from services.evaluation_dispatcher import EvaluationDispatcher
from utils.interview_session import SessionStatus

logger = get_logger("services.evaluation")


@dataclass
class EvaluationAgentFactories:
    """Optional zero-arg factories for each of the seven agents this
    service calls - the same wiring-seam pattern
    `InterviewService.interviewer_factory` established in Chunk 1, bundled
    into one object because seven separate `app.state.*_factory` attributes
    would be unwieldy where one or two were manageable before. None (the
    default for every field) means "construct the real agent", exactly as
    every other factory seam in this backend already means.
    """

    technical_evaluator: Optional[Callable[[], TechnicalEvaluatorAgent]] = None
    behavioral_evaluator: Optional[Callable[[], BehavioralEvaluatorAgent]] = None
    resume_auditor: Optional[Callable[[], ResumeAuditorAgent]] = None
    integrity: Optional[Callable[[], IntegrityAgent]] = None
    bias_checker: Optional[Callable[[], BiasCheckerAgent]] = None
    scoring: Optional[Callable[[], ScoringAgent]] = None
    report_generator: Optional[Callable[[], ReportGeneratorAgent]] = None


def _unwrap(agent_result: dict) -> dict:
    """Identical in spirit to orchestration/graph.py's own `_unwrap` -
    `BaseAgent.run()`'s envelope is `{"result": ..., "audit_log": ...}` on
    success or `{"result": None, "audit_log": ..., "error": str}` on
    failure; this never raises either way (P0-4: agents never let a failure
    look like success, they report it explicitly in the envelope)."""
    return agent_result.get("result") or {}


def _agent_error(agent_result: dict, result: dict) -> str:
    return result.get("error") or agent_result.get("error") or "unknown error"


class EvaluationService:
    """Use cases for evaluation jobs.

    Constructed per request (cheap - holds references only), like every
    other service in this backend. `dispatcher` is the one exception to
    "holds no state": it MUST be the container's long-lived singleton (see
    services/evaluation_dispatcher.py), never a fresh instance, or
    scheduled background work would have nothing keeping it alive past the
    request that triggered it.
    """

    def __init__(
        self,
        *,
        evaluation_repository: EvaluationRepository,
        session_repository: SessionRepository,
        transcript_repository: TranscriptRepository,
        application_repository: ApplicationRepository,
        dispatcher: EvaluationDispatcher,
        agent_factories: Optional[EvaluationAgentFactories] = None,
    ) -> None:
        self._evaluations = evaluation_repository
        self._sessions = session_repository
        self._transcripts = transcript_repository
        self._applications = application_repository
        self._dispatcher = dispatcher
        self._agents = agent_factories or EvaluationAgentFactories()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def trigger_evaluation(self, session_id: str) -> EvaluationJob:
        """Create (if one doesn't already exist) and schedule the
        evaluation for a sealed interview session, returning immediately -
        never waits for the agent pipeline (Step 5).

        Idempotent (Step 6): calling this again for the same session_id
        while one is PENDING, RUNNING, or COMPLETED returns the EXISTING
        job untouched and schedules nothing new. A FAILED job is also
        returned as-is - retrying it is a distinct, explicit operation
        (`retry_evaluation`), never automatic, so a duplicate trigger can
        never turn into a duplicate retry attempt.

        Raises `ConflictError` if the session is not sealed, or its
        transcript is not yet durably persisted (Chunk 1's
        `transcript_persisted` - evaluating a transcript that might not
        survive a restart is not "no evaluation system", it is evaluating
        the wrong source of truth). Raises `NotFoundError` if the session
        does not exist.
        """
        session_record = await self._sessions.get(session_id)
        if session_record is None:
            raise NotFoundError(
                "No session found for the given session_id",
                internal_detail=f"session_id={session_id!r} not found",
            )
        if session_record.status != SessionStatus.SEALED:
            raise ConflictError(
                "Evaluation can only be triggered for a sealed interview session",
                internal_detail=f"session_id={session_id!r} status={session_record.status.value!r}",
            )
        if (
            session_record.interview_id is None
            or session_record.job_description is None
            or session_record.parsed_resume is None
        ):
            # Defensive only: every record saved after Chunk 3 always has
            # these. A record missing them cannot be evaluated any more than
            # it could be restored (services/interview_service.py).
            raise ConflictError(
                "This session has no restorable snapshot and cannot be evaluated",
                internal_detail=f"session_id={session_id!r} is missing its Chunk 3 snapshot",
            )
        transcript = await self._transcripts.get(session_record.interview_id)
        if transcript is None:
            raise ConflictError(
                "The interview transcript has not been durably persisted yet",
                internal_detail=(
                    f"session_id={session_id!r} interview_id={session_record.interview_id!r} "
                    "has no transcript in TranscriptRepository yet"
                ),
            )

        candidate_job = EvaluationJob(
            evaluation_id=f"eval_{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            interview_id=session_record.interview_id,
            candidate_id=session_record.candidate_id,
            job_id=session_record.job_id,
            application_id=session_record.application_id,
            status=EvaluationStatus.PENDING,
        )
        job = await self._evaluations.create_if_absent_for_session(candidate_job)

        if job.evaluation_id != candidate_job.evaluation_id:
            # Someone else already triggered (or completed/failed) this
            # session's evaluation - return their job, schedule nothing.
            logger.info(
                "evaluation already exists for session, not re-triggering",
                extra=log_context(event="evaluation_trigger_deduplicated",
                                  session_id=session_id, evaluation_id=job.evaluation_id,
                                  status=job.status.value),
            )
            return job

        logger.info(
            "evaluation job created and scheduled",
            extra=log_context(event="evaluation_created", session_id=session_id,
                              evaluation_id=job.evaluation_id),
        )
        self._dispatcher.schedule(job.evaluation_id, lambda: self._run(job.evaluation_id))
        return job

    async def retry_evaluation(self, evaluation_id: str) -> EvaluationJob:
        """Explicitly re-run a FAILED evaluation (Step 6: "FAILED -> allow
        controlled retry"). Never automatic - a caller (an operator, or a
        future admin endpoint) must ask for it by evaluation_id.

        Raises `NotFoundError` if unknown, `ConflictError` if the job is
        not currently FAILED (retrying a PENDING/RUNNING/COMPLETED job would
        either duplicate in-flight work or discard a real result).
        """
        job = await self._evaluations.get(evaluation_id)
        if job is None:
            raise NotFoundError(
                "No evaluation found for the given evaluation_id",
                internal_detail=f"evaluation_id={evaluation_id!r} not found",
            )
        if job.status != EvaluationStatus.FAILED:
            raise ConflictError(
                "Only a failed evaluation can be retried",
                internal_detail=f"evaluation_id={evaluation_id!r} status={job.status.value!r}",
            )

        reset = job.model_copy(update={
            "status": EvaluationStatus.PENDING, "error": None, "warnings": [],
            "result": None, "started_at": None, "completed_at": None,
        })
        stored = await self._evaluations.save(reset)
        logger.info(
            "evaluation retry scheduled",
            extra=log_context(event="evaluation_retry", evaluation_id=evaluation_id),
        )
        self._dispatcher.schedule(evaluation_id, lambda: self._run(evaluation_id))
        return stored

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_evaluation(self, evaluation_id: str) -> EvaluationJob:
        job = await self._evaluations.get(evaluation_id)
        if job is None:
            raise NotFoundError(
                "No evaluation found for the given evaluation_id",
                internal_detail=f"evaluation_id={evaluation_id!r} not found",
            )
        return job

    async def get_evaluation_for_session(self, session_id: str) -> Optional[EvaluationJob]:
        """None means "not triggered yet" - a normal, valid state (the
        interview may still be in progress, or its transcript may not have
        persisted yet), not an error. Distinct from `get_evaluation`, which
        404s on absence - see api/routes/interview.py's
        `GET /sessions/{id}/evaluation` for why this method's `None` maps
        to a 200 with an empty body, not a 404."""
        return await self._evaluations.get_for_session(session_id)

    # ------------------------------------------------------------------
    # Background execution
    # ------------------------------------------------------------------

    async def _run(self, evaluation_id: str) -> None:
        """The dispatcher's scheduled coroutine. Never raises - every
        outcome (including a bug in this method itself) is captured and the
        job is always left in a terminal, honest state; `AsyncTaskEvaluationDispatcher`'s
        done-callback logs anything that somehow still escapes as a louder
        signal that this contract was broken.
        """
        job = await self._evaluations.get(evaluation_id)
        if job is None:
            logger.error(
                "PERSISTENCE_FAILURE evaluation job vanished before it could run",
                extra=log_context(event="evaluation_missing", evaluation_id=evaluation_id),
            )
            return

        running = job.model_copy(
            update={"status": EvaluationStatus.RUNNING, "started_at": datetime.now(timezone.utc)}
        )
        await self._evaluations.save(running)

        # Every branch below finishes from `running`, not `job` - it
        # carries `started_at`, and a COMPLETED/FAILED job must never lose
        # that timestamp just because it was set in a separate save.
        try:
            session_record = await self._sessions.get(running.session_id)
            transcript = await self._transcripts.get(running.interview_id)
            matching_score = await self._resolve_matching_score(running)

            if session_record is None or transcript is None:
                await self._finish_failed(running, "session record or transcript disappeared before evaluation ran")
                return
            if matching_score is None:
                await self._finish_failed(
                    running,
                    "no matching score is available for this application - "
                    "ScoringAgent requires one and none could be found",
                )
                return

            report, warnings, fatal_reason = await self._run_agents(
                job_description=session_record.job_description,
                parsed_resume=session_record.parsed_resume,
                transcript=transcript,
                matching_score=matching_score,
                candidate_id=running.candidate_id,
                run_id=running.evaluation_id,
            )
            if fatal_reason is not None:
                await self._finish_failed(running, fatal_reason, warnings=warnings)
                return

            completed = running.model_copy(update={
                "status": EvaluationStatus.COMPLETED,
                "result": report,
                "warnings": warnings,
                "error": None,
                "completed_at": datetime.now(timezone.utc),
            })
            await self._evaluations.save(completed)
            logger.info(
                "evaluation completed",
                extra=log_context(event="evaluation_completed", evaluation_id=evaluation_id,
                                  warning_count=len(warnings)),
            )
        except Exception as exc:  # noqa: BLE001 - a background task must never raise
            await self._finish_failed(running, str(exc), unexpected=True)

    async def _finish_failed(
        self, job: EvaluationJob, reason: str, *, warnings: Optional[List[str]] = None, unexpected: bool = False,
    ) -> None:
        failed = job.model_copy(update={
            "status": EvaluationStatus.FAILED,
            "error": reason,
            "warnings": warnings or [],
            "completed_at": datetime.now(timezone.utc),
        })
        await self._evaluations.save(failed)
        log = logger.error if unexpected else logger.warning
        log(
            "evaluation failed: %s",
            reason,
            exc_info=unexpected,
            extra=log_context(event="evaluation_failed", evaluation_id=job.evaluation_id),
        )

    async def _resolve_matching_score(self, job: EvaluationJob) -> Optional[MatchingScore]:
        if job.application_id is None:
            return None
        application = await self._applications.get(job.application_id)
        return application.matching_score if application is not None else None

    # ------------------------------------------------------------------
    # The existing agent pipeline, called directly (see module docstring)
    # ------------------------------------------------------------------

    async def _run_agents(
        self,
        *,
        job_description: JobDescription,
        parsed_resume: ParsedResume,
        transcript: InterviewTranscript,
        matching_score: MatchingScore,
        candidate_id: str,
        run_id: str,
    ) -> Tuple[Optional[CandidateReport], List[str], Optional[str]]:
        """Returns (report, warnings, fatal_reason). Exactly one of
        `report`/`fatal_reason` is meaningful: `report` is not None iff
        `fatal_reason` is None.

        Stage grouping and required/optional status mirror
        orchestration/graph.py's own nodes exactly:
          stage 1 (concurrent):  technical, behavioral, resume_audit, integrity
                                  - node_run_parallel_evaluations
          stage 2 (after 1):     bias_check - node_run_bias_check (needs
                                  stage 1's technical/behavioral scores)
          stage 3 (after 1):     scoring - node_score_candidates (needs
                                  stage 1's technical/behavioral + the
                                  existing matching_score; does NOT depend
                                  on stage 2, but is kept sequential after it
                                  because the existing graph's own edges are
                                  bias_check -> score, and Step 7 says
                                  preserve existing behavior rather than
                                  exploit a concurrency opportunity the
                                  existing pipeline itself does not take)
          stage 4 (after 3):     report_generator - node_generate_reports
        """
        technical_evaluator = (
            self._agents.technical_evaluator() if self._agents.technical_evaluator
            else TechnicalEvaluatorAgent()
        )
        behavioral_evaluator = (
            self._agents.behavioral_evaluator() if self._agents.behavioral_evaluator
            else BehavioralEvaluatorAgent()
        )
        resume_auditor = (
            self._agents.resume_auditor() if self._agents.resume_auditor else ResumeAuditorAgent()
        )
        integrity_agent = (
            self._agents.integrity() if self._agents.integrity else IntegrityAgent()
        )
        bias_checker = (
            self._agents.bias_checker() if self._agents.bias_checker else BiasCheckerAgent()
        )
        scoring_agent = self._agents.scoring() if self._agents.scoring else ScoringAgent()
        report_generator = (
            self._agents.report_generator() if self._agents.report_generator
            else ReportGeneratorAgent()
        )

        warnings: List[str] = []

        # -- stage 1: concurrent, matches node_run_parallel_evaluations ---
        technical_result, behavioral_result, resume_audit_result, integrity_result = await asyncio.gather(
            technical_evaluator.run(
                run_id=run_id, job_description=job_description, parsed_resume=parsed_resume,
                interview_transcript=transcript,
            ),
            behavioral_evaluator.run(
                run_id=run_id, job_description=job_description, parsed_resume=parsed_resume,
                interview_transcript=transcript,
            ),
            resume_auditor.run(
                run_id=run_id, parsed_resume=parsed_resume, interview_transcript=transcript,
            ),
            integrity_agent.run(
                run_id=run_id, parsed_resume=parsed_resume, interview_transcript=transcript,
            ),
        )

        technical_evaluation: Optional[TechnicalEvaluation] = _unwrap(technical_result).get("technical_evaluation")
        if technical_evaluation is None:
            return None, warnings, (
                f"technical evaluation failed: "
                f"{_agent_error(technical_result, _unwrap(technical_result))}"
            )

        behavioral_evaluation: Optional[BehavioralEvaluation] = _unwrap(behavioral_result).get("behavioral_evaluation")
        if behavioral_evaluation is None:
            return None, warnings, (
                f"behavioral evaluation failed: "
                f"{_agent_error(behavioral_result, _unwrap(behavioral_result))}"
            )

        resume_audit_unwrapped = _unwrap(resume_audit_result)
        claim_verifications: Optional[List[ClaimVerification]] = resume_audit_unwrapped.get("claim_verifications")
        if claim_verifications is None:
            warnings.append(
                f"resume audit failed (non-fatal): "
                f"{_agent_error(resume_audit_result, resume_audit_unwrapped)}"
            )

        integrity_unwrapped = _unwrap(integrity_result)
        integrity_evaluation: Optional[IntegrityEvaluation] = integrity_unwrapped.get("integrity_evaluation")
        if integrity_evaluation is None:
            warnings.append(
                f"integrity check failed (non-fatal): "
                f"{_agent_error(integrity_result, integrity_unwrapped)}"
            )

        # -- stage 2: matches node_run_bias_check --------------------------
        bias_result = await bias_checker.run(
            run_id=run_id, candidate_id=candidate_id, job_id=job_description.job_id,
            interview_transcript=transcript,
            technical_score=technical_evaluation.technical_score,
            behavioral_score=behavioral_evaluation.behavioral_score,
            # Reuses orchestration/graph.py's own helper for building the
            # evaluator-rationale text bias check audits (P0-5) - the exact
            # same evidence-section construction, not a reimplementation.
            evaluation_sections=_build_evaluation_sections(
                technical_evaluation, behavioral_evaluation, claim_verifications, integrity_evaluation,
            ),
        )
        bias_unwrapped = _unwrap(bias_result)
        bias_audit: Optional[BiasAudit] = bias_unwrapped.get("bias_audit")
        if bias_audit is None:
            warnings.append(f"bias check failed (non-fatal): {_agent_error(bias_result, bias_unwrapped)}")

        # -- stage 3: matches node_score_candidates ------------------------
        scoring_result = await scoring_agent.run(
            run_id=run_id, job_description=job_description,
            technical_evaluation=technical_evaluation, behavioral_evaluation=behavioral_evaluation,
            matching_score=matching_score, candidate_id=candidate_id,
        )
        scoring_unwrapped = _unwrap(scoring_result)
        candidate_scores = scoring_unwrapped.get("candidate_scores")
        if candidate_scores is None:
            return None, warnings, f"scoring failed: {_agent_error(scoring_result, scoring_unwrapped)}"

        # -- stage 4: matches node_generate_reports ------------------------
        report_result = await report_generator.run(
            run_id=run_id, candidate_scores=candidate_scores, parsed_resume=parsed_resume,
            technical_evaluation=technical_evaluation, behavioral_evaluation=behavioral_evaluation,
            bias_audit=bias_audit, integrity_evaluation=integrity_evaluation,
            resume_audits=claim_verifications,
        )
        report_unwrapped = _unwrap(report_result)
        candidate_report: Optional[CandidateReport] = report_unwrapped.get("candidate_report")
        if candidate_report is None:
            return None, warnings, f"report generation failed: {_agent_error(report_result, report_unwrapped)}"

        return candidate_report, warnings, None


__all__ = ["EvaluationAgentFactories", "EvaluationService"]
