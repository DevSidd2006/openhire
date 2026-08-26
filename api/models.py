"""
P5 Phase 4: API request/response models.

These are transport-layer DTOs, not domain models - domain data (JobDescription,
ParsedResume, InterviewQuestion, EvidenceItem, InterviewState) comes straight
from the existing schemas (schemas/job.py, schemas/resume.py,
schemas/interview.py, schemas/evaluation.py) and is never duplicated here.
Response models only ever expose a deliberately narrow VIEW of those domain
objects - never the raw pydantic object itself, and never internal fields a
candidate should not see mid-interview (e.g. InterviewQuestion.reason, which
can contain text like "SQL evidence is insufficient (confidence 0.32)" - the
adaptive engine's own internal justification for asking this question).
"""
from typing import List, Optional

from pydantic import BaseModel, Field

from repositories.interfaces import EvaluationStatus
from schemas.interview import InterviewQuestion, InterviewState
from schemas.job import JobDescription
from schemas.resume import ParsedResume
from utils.interview_session import AnswerSubmissionResult, SessionStatus


# ---------------------------------------------------------------------------
# Candidate-facing views of domain objects (never the raw model)
# ---------------------------------------------------------------------------

class QuestionView(BaseModel):
    """A candidate-facing view of InterviewQuestion. Deliberately omits
    `reason` and any future internal-justification field - the adaptive
    engine's own reasoning for asking this question is not the candidate's
    to see (P5 Phase 11: controlled representation)."""
    question_id: str
    question_text: str
    competency: Optional[str] = None
    difficulty: Optional[str] = None
    question_type: Optional[str] = None

    @classmethod
    def from_domain(cls, question: Optional[InterviewQuestion]) -> Optional["QuestionView"]:
        if question is None:
            return None
        return cls(
            question_id=question.question_id,
            question_text=question.question_text,
            competency=question.competency,
            difficulty=question.difficulty,
            question_type=question.question_type,
        )


class EvidenceView(BaseModel):
    """A candidate-facing view of one EvidenceItem for the answer they just
    submitted. Omits `agent`, `source_type`, `created_at`, and `text`
    (`text` is just their own answer verbatim, already known to the caller -
    repeating it back adds nothing and this stays a summary view, not a
    1:1 dump of the internal evidence record)."""
    evidence_id: str
    competency: Optional[str] = None
    evidence_type: str
    relevance: float
    explanation: str

    @classmethod
    def from_domain(cls, evidence) -> "EvidenceView":
        return cls(
            evidence_id=evidence.evidence_id,
            competency=evidence.competency,
            evidence_type=evidence.evidence_type,
            relevance=evidence.relevance,
            explanation=evidence.explanation,
        )


class ProgressView(BaseModel):
    """A summary of interview progress - not the full InterviewState (which
    carries internal fields like competency_signals/follow_up_counts)."""
    questions_asked: int
    questions_answered: int
    max_questions: int
    covered_competencies: List[str]

    @classmethod
    def from_domain(cls, state: InterviewState) -> "ProgressView":
        return cls(
            questions_asked=state.questions_asked,
            questions_answered=state.questions_answered,
            max_questions=state.max_questions,
            covered_competencies=list(state.covered_competencies),
        )


# ---------------------------------------------------------------------------
# POST /sessions
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    """Reuses the existing JobDescription/ParsedResume schemas directly
    (P5 Phase 4: "do not duplicate Candidate or JobDescription schemas
    unnecessarily") rather than re-declaring their fields here.

    candidate_id/job_id are required as explicit top-level fields (matching
    InterviewSessionRunner's own constructor, which takes candidate_id as an
    override independent of parsed_resume.candidate_id) and are validated
    against job_description.job_id/parsed_resume.candidate_id in the route
    handler - a mismatch is a 400, not a 422, since it's a business-rule
    check, not a schema/type error.
    """
    candidate_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    job_description: JobDescription
    parsed_resume: ParsedResume
    max_questions: Optional[int] = Field(default=None, ge=1, le=50)
    # Chunk 2 bridge (Application -> Interview): optional and additive.
    # Omitted (the default), this endpoint behaves exactly as it always
    # has. Supplied, api/routes/interview.py verifies the named
    # application's job_id/candidate_id match this request and that it is
    # SHORTLISTED, then links the resulting session_id onto it - see
    # services/application_service.py:link_interview_session. The
    # application's own job_description/parsed_resume are still NOT looked
    # up automatically; the caller supplies them here exactly as before
    # (they can be fetched via GET /jobs/{id} and GET /candidates/{id}).
    application_id: Optional[str] = Field(default=None, min_length=1)


class CreateSessionResponse(BaseModel):
    session_id: str
    status: SessionStatus
    current_question: Optional[QuestionView] = None
    # Echoes CreateSessionRequest.application_id when one was supplied and
    # successfully linked; None otherwise (including when no application_id
    # was given at all) - additive, never populated for a pre-Chunk-2 caller.
    application_id: Optional[str] = None


# ---------------------------------------------------------------------------
# GET /sessions/{session_id}
# ---------------------------------------------------------------------------

class SessionStateResponse(BaseModel):
    session_id: str
    status: SessionStatus
    current_question: Optional[QuestionView] = None
    progress: ProgressView
    termination_reason: Optional[str] = None
    # None while the session is not yet sealed (nothing to persist yet).
    # Once sealed: True means the transcript is confirmed present in
    # TranscriptRepository; False means a prior write failed and is being
    # retried on this read (api/routes/interview.py) - never omitted or
    # defaulted to True on a failure, per the Chunk-1 persistence
    # correction (services/interview_service.py's module docstring).
    transcript_persisted: Optional[bool] = None
    # Chunk 4: populated once evaluation has been triggered for this
    # session (only possible once transcript_persisted is True) - None
    # until then, including for a session with no application_id at all
    # (evaluation cannot run without one - see
    # services/evaluation_service.py's module docstring).
    evaluation_id: Optional[str] = None
    evaluation_status: Optional[EvaluationStatus] = None


# ---------------------------------------------------------------------------
# POST /sessions/{session_id}/answers
# ---------------------------------------------------------------------------

class SubmitAnswerRequest(BaseModel):
    answer_text: str = Field(min_length=1, max_length=10_000)


class SubmitAnswerResponse(BaseModel):
    answer_id: str
    evidence: EvidenceView
    next_question: Optional[QuestionView] = None
    status: SessionStatus
    termination_reason: Optional[str] = None
    # See SessionStateResponse.transcript_persisted. Populated by the route
    # handler (not by from_domain below) because the persistence outcome is
    # an application-layer fact, not something AnswerSubmissionResult (the
    # interview engine's own return value) carries or should carry.
    transcript_persisted: Optional[bool] = None
    # See SessionStateResponse.evaluation_id/evaluation_status.
    evaluation_id: Optional[str] = None
    evaluation_status: Optional[EvaluationStatus] = None

    @classmethod
    def from_domain(cls, result: AnswerSubmissionResult) -> "SubmitAnswerResponse":
        return cls(
            answer_id=result.answer.question_id,
            evidence=EvidenceView.from_domain(result.evidence),
            next_question=QuestionView.from_domain(result.next_question),
            status=result.session_status,
            termination_reason=result.termination_reason,
        )


# ---------------------------------------------------------------------------
# POST /sessions/{session_id}/finish
# ---------------------------------------------------------------------------

class FinishSessionResponse(BaseModel):
    session_id: str
    status: SessionStatus
    termination_reason: Optional[str] = None
    # See SessionStateResponse.transcript_persisted.
    transcript_persisted: Optional[bool] = None
    # See SessionStateResponse.evaluation_id/evaluation_status.
    evaluation_id: Optional[str] = None
    evaluation_status: Optional[EvaluationStatus] = None


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    """Liveness only.

    Kept to exactly one field on purpose. This endpoint is polled
    continuously by load balancers and container orchestrators, so it must
    stay the cheapest response in the service, and it must not describe the
    service's configuration to anyone who can reach it. The operational
    summary (environment, provider names, whether persistence is durable)
    lives on `GET /` instead - see api/app.py.
    """
    status: str = "ok"


# ---------------------------------------------------------------------------
# Error responses (P5 Phase 8)
# ---------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    """Consistent JSON error shape for every error this API returns.
    `detail` is always a client-safe message - never a raw exception,
    stack trace, filesystem path, or provider credential (see
    api/errors.py)."""
    error: str
    detail: str
