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


class ResumeEducationExtract(BaseModel):
    """One education entry as reported directly by the resume-parsing LLM
    call - deliberately ALL-OPTIONAL, unlike schemas.resume.Education
    (institution/degree/field_of_study required there). An extractor can
    legitimately be uncertain about any one of these; ResumeParserAgent
    drops an entry that doesn't clear Education's required fields rather
    than fabricating a placeholder for a missing one (P7 Phase 2/4)."""
    institution: Optional[str] = None
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    graduation_year: Optional[int] = None
    gpa: Optional[float] = None
    honors: Optional[str] = None


class ResumeWorkExperienceExtract(BaseModel):
    """One work-experience entry as reported directly by the LLM -
    all-optional for the same reason as ResumeEducationExtract; e.g.
    schemas.resume.WorkExperience.start_year is required there, but a
    resume may state a role with no clear start year, and dropping that
    ONE entry must not be forced to also nuke every other, correctly-
    extracted entry (which a single failed nested-model validation inside
    a single retryable structured-output call otherwise would)."""
    company: Optional[str] = None
    position: Optional[str] = None
    start_year: Optional[int] = None
    end_year: Optional[int] = None
    is_current: bool = False
    duration_months: Optional[int] = None
    description: Optional[str] = None
    responsibilities: List[str] = Field(default_factory=list)
    achievements: List[str] = Field(default_factory=list)


class ResumeProjectExtract(BaseModel):
    """schemas.resume.Project requires name+description; all-optional here
    for the same reason as above."""
    name: Optional[str] = None
    description: Optional[str] = None
    technologies: List[str] = Field(default_factory=list)
    url: Optional[str] = None
    role: Optional[str] = None
    outcome: Optional[str] = None


class ResumeCertificationExtract(BaseModel):
    """schemas.resume.Certification requires only `name`; kept optional
    here too so a certification with an unclear name is dropped rather
    than silently defaulted to an empty string that would then pass
    Certification's own validation as if it meant something."""
    name: Optional[str] = None
    issuer: Optional[str] = None
    issue_date: Optional[str] = None
    expiration_date: Optional[str] = None
    credential_url: Optional[str] = None


class ResumeParseResult(BaseModel):
    """Expected LLM output for prompts/resume_parser.md (P7).

    ONLY what the LLM is responsible for extracting from resume text - no
    candidate_id/candidate_name (supplied by the caller, never invented by
    the LLM), no raw_text/parse_date/source_format (system-generated by the
    agent, see ResumeParserAgent._build_parsed_resume).

    Every nested list uses the all-optional *Extract variant above, not the
    domain model directly - the domain models (schemas.resume.Education
    etc.) enforce required fields that make sense for a COMPLETE record but
    not for what an extractor may legitimately be uncertain about mid-
    resume; ResumeParserAgent's business-validation step (P7 Phase 2's
    "deterministic/business validation" stage) decides, per entry, whether
    enough was extracted to construct the real domain object, and drops
    (never fabricates-to-fill) any entry that doesn't clear that bar.
    """
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    summary: Optional[str] = None
    education: List[ResumeEducationExtract] = Field(default_factory=list)
    work_experience: List[ResumeWorkExperienceExtract] = Field(default_factory=list)
    projects: List[ResumeProjectExtract] = Field(default_factory=list)
    certifications: List[ResumeCertificationExtract] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    technologies: List[str] = Field(default_factory=list)
    languages: List[str] = Field(default_factory=list)
    total_experience_years: Optional[float] = None


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
