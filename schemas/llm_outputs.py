"""
Pydantic models describing exactly what an LLM call is expected to produce
for each migrated agent (P1: reliable structured output).

These are deliberately NARROWER than the full downstream agent-output
schemas in schemas/evaluation.py / schemas/job.py - those also carry fields
the agent computes itself (IDs, evidence objects resolved against the real
transcript, job_id threaded from elsewhere, timestamps...) that an LLM must
never be asked to invent.

Each model here serves two purposes:
1. `Model.model_json_schema()` is passed as the `schema` argument to
   LLMProvider.generate_structured(), so the JSON Schema is always derived
   from the Pydantic model - never hand-written and kept in sync separately.
2. `Model.model_validate(raw_dict)` validates the provider's response before
   the agent converts it into its real output schema (see
   agents/base.py:BaseAgent.call_llm_structured(validate=...)).

Provider-level validation ("is this valid JSON matching the requested
shape?") happens in providers/llm/*.py. Structural/schema validation ("is
the score in range? is severity one of the allowed values?") happens here,
via Pydantic. Semantic/business validation ("does this evidence actually
support the score?") stays in the agent - these models never make that
judgment.
"""
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field

from schemas.interview import QuestionType


class CompetencyJudgment(BaseModel):
    """One competency's score as reported directly by an evaluator LLM call -
    shared by TechnicalEvaluationResult and BehavioralEvaluationResult since
    both ask the LLM for the same per-competency shape."""
    score: float = Field(ge=0.0, le=10.0)
    confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    evidence_question_number: Optional[int] = None
    explanation: str = ""


class TechnicalEvaluationResult(BaseModel):
    """Expected LLM output for prompts/technical_evaluator.md."""
    technical_score: float = Field(ge=0.0, le=10.0)
    competency_scores: Dict[str, CompetencyJudgment] = Field(default_factory=dict)
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    explanation: str = "Technical evaluation complete"
    confidence: float = Field(default=0.80, ge=0.0, le=1.0)


class BehavioralEvaluationResult(BaseModel):
    """Expected LLM output for prompts/behavioral_evaluator.md."""
    behavioral_score: float = Field(ge=0.0, le=10.0)
    communication: float = Field(default=7.5, ge=0.0, le=10.0)
    problem_solving: float = Field(default=7.5, ge=0.0, le=10.0)
    teamwork: float = Field(default=7.0, ge=0.0, le=10.0)
    adaptability: float = Field(default=7.0, ge=0.0, le=10.0)
    competency_scores: Dict[str, CompetencyJudgment] = Field(default_factory=dict)
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    explanation: str = "Behavioral evaluation complete"
    confidence: float = Field(default=0.80, ge=0.0, le=1.0)


class ClaimVerificationResult(BaseModel):
    """Expected LLM output for one prompts/resume_auditor.md call."""
    verification_status: Literal[
        "supported", "partially_supported", "inconsistent",
        "insufficient_evidence", "requires_human_review",
    ] = "insufficient_evidence"
    confidence: float = Field(default=0.60, ge=0.0, le=1.0)
    evidence_question_number: Optional[int] = None
    explanation: str = "Claim could not be verified"
    requires_human_review: bool = False


class IntegrityFlagResult(BaseModel):
    flag_type: str = "other"
    severity: Literal["low", "medium", "high"] = "medium"
    confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    evidence_question_numbers: List[int] = Field(default_factory=list)
    description: str = ""
    requires_human_review: bool = True


class IntegrityCheckResult(BaseModel):
    """Expected LLM output for prompts/integrity.md."""
    flags: List[IntegrityFlagResult] = Field(default_factory=list)
    overall_integrity: Literal["clear", "flagged", "requires_review"] = "clear"
    explanation: str = "Integrity analysis complete"
    confidence: float = Field(default=0.80, ge=0.0, le=1.0)


class BiasFlagResult(BaseModel):
    bias_type: str = "other"
    severity: Literal["low", "medium", "high"] = "low"
    confidence: float = Field(default=0.60, ge=0.0, le=1.0)
    evidence_source: Optional[Literal[
        "technical_evaluation", "behavioral_evaluation",
        "resume_audit", "integrity", "technical_score", "behavioral_score",
    ]] = None
    description: str = ""
    recommendation: str = "Monitor this category"


class BiasCheckResult(BaseModel):
    """Expected LLM output for prompts/bias_checker.md."""
    flags: List[BiasFlagResult] = Field(default_factory=list)
    fairness_status: Literal["pass", "fair", "flagged", "requires_review"] = "pass"
    explanation: str = "Bias check complete"
    confidence: float = Field(default=0.75, ge=0.0, le=1.0)


class InterviewQuestionResult(BaseModel):
    """Expected LLM output for one prompts/interviewer.md call."""
    question_text: str = "Can you tell us about your experience?"
    category: str = "general"
    competency: Optional[str] = None
    difficulty: Optional[str] = "medium"
    reason: Optional[str] = None
    expected_duration_seconds: Optional[int] = 60


class AnswerEvaluationResult(BaseModel):
    """Expected LLM output for one prompts/answer_evaluator.md call (P3).

    Scores exactly one candidate answer against exactly one target
    competency - the caller already knows which question/answer/competency
    triple this is about (see InterviewerAgent.evaluate_answer), so unlike
    TechnicalEvaluationResult this is never keyed by competency name and
    never needs an evidence_question_number (the evidence IS the answer
    being evaluated; utils/adaptive_interview builds the EvidenceItem
    directly from it, never from a resolved transcript lookup).

    `score` and `confidence` have NO default - an evaluator call that omits
    them must fail validation and retry/error (P1/P3 Phase 12 policy),
    never silently score as if the answer were adequate.
    """
    score: float = Field(ge=0.0, le=10.0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_status: Literal["supported", "insufficient"] = "insufficient"
    is_vague: bool = False
    missing_detail: Optional[str] = None
    explanation: str = ""


class AdaptiveQuestionResult(BaseModel):
    """Expected LLM output for one prompts/adaptive_interviewer.md call (P3).

    Deliberately separate from InterviewQuestionResult (used by the pre-P3
    batch planner): that model defaults question_text to a fabricated
    placeholder question, which P3 Phase 12 explicitly forbids for the
    adaptive path - here question_text and question_type are REQUIRED, so a
    response missing either fails schema validation and is retried/raised
    rather than silently generating a fake question.
    """
    question_text: str = Field(min_length=1)
    question_type: QuestionType
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    reason: str = ""
    expected_duration_seconds: Optional[int] = 60


class RawCompetency(BaseModel):
    """A competency exactly as reported by the JD-analysis LLM call -
    deliberately UNCONSTRAINED on weight range/sum (unlike
    schemas.job.Competency) so the agent's existing
    validate_competencies()/_normalize_competencies() business-logic repair
    step (agents/jd_analyzer/agent.py) still runs on it, exactly as before
    this migration. The stricter, range-checked Competency is only
    constructed AFTER that repair step."""
    name: str
    weight: float = 0.0


class JDAnalysisResult(BaseModel):
    """Expected LLM output for prompts/jd_analyzer.md. job_id and
    `description` are NOT included here - the agent fills those in itself
    from its own inputs, never from the LLM."""
    title: str = "Unstructured Job"
    department: Optional[str] = None
    level: Optional[str] = None
    required_skills: List[str] = Field(default_factory=list)
    preferred_skills: List[str] = Field(default_factory=list)
    required_qualifications: List[str] = Field(default_factory=list)
    preferred_qualifications: List[str] = Field(default_factory=list)
    experience_years: Optional[int] = None
    responsibilities: List[str] = Field(default_factory=list)
    competencies: List[RawCompetency] = Field(default_factory=list)
    interview_topics: List[str] = Field(default_factory=list)
