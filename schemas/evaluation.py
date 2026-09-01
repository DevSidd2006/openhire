"""
Evidence tracking and evaluation related schemas.
"""
from datetime import datetime, timezone
from typing import List, Literal, Optional, Any
from pydantic import BaseModel, Field

from schemas.rubric import CompetencyVerdict

# The full evidence-type vocabulary (P2). "supporting"/"contradicting" mirror
# the judgment an evidence item backs; "insufficient" marks a genuine
# absence-of-evidence finding (e.g. "no transcript exchange addressed this
# competency") - a factual coverage statement, never a fabricated claim about
# the candidate. Kept as a plain Literal (not a separate enum type) to match
# how the rest of this module already represents small closed vocabularies
# (severity, verification_status, etc.).
EvidenceType = Literal["supporting", "contradicting", "insufficient"]


class EvidenceItem(BaseModel):
    """Traceable evidence for any evaluation claim.

    candidate_id is a redundant-by-design safety net: every EvidenceItem is
    normally reached by navigating from a specific candidate's evaluation
    (CompetencyScore.evidence, IntegrityFlag.evidence, ...), but stamping the
    candidate directly onto the evidence item lets validation catch a
    cross-candidate mixup (utils/evidence.py:validate_evidence_belongs_to_candidate)
    even if it somehow ended up in the wrong place.
    """
    evidence_id: str
    candidate_id: Optional[str] = None  # who this evidence is about
    source_type: str  # "transcript", "resume", "derived"
    source_id: Optional[str] = None  # question_id, resume_section, etc.
    question_id: Optional[str] = None
    answer_id: Optional[str] = None  # same value as question_id in this
    # codebase today (InterviewAnswer.question_id is the pairing key - there
    # is no separate answer identifier), kept as its own field so evidence is
    # self-describing without callers needing to know that detail.
    evidence_type: EvidenceType = "supporting"
    competency: Optional[str] = None  # criterion/competency this evidence relates to, when applicable
    timestamp_start: Optional[float] = None  # In seconds
    timestamp_end: Optional[float] = None
    text: str = Field(min_length=1)  # The actual evidence text - never empty, never fabricated
    relevance: float = Field(ge=0.0, le=1.0, description="Relevance score")
    agent: str  # Which agent generated this
    explanation: str  # Why this is evidence
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class CompetencyScore(BaseModel):
    """Score for a single competency."""
    competency_name: str
    score: float = Field(ge=0.0, le=10.0)
    max_score: float = 10.0
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: List[EvidenceItem] = Field(default_factory=list)
    explanation: str
    feedback: Optional[str] = None

    # "supported": at least one real, grounded EvidenceItem backs this score.
    # "insufficient": an evaluator reported a score/confidence for this
    # competency but no transcript evidence could be resolved for it - the
    # score is kept (for transparency about what the LLM claimed) but is
    # explicitly marked as NOT evidence-backed, so scoring/reporting can
    # treat it differently rather than silently counting it as a normal
    # supported result (see ScoringAgent._compute_rubric_score, P2 Phase 5/11).
    evidence_status: Literal["supported", "insufficient"] = "supported"


class TechnicalEvaluation(BaseModel):
    """Technical skills evaluation."""
    evaluation_id: str
    candidate_id: str
    job_id: str
    interview_id: str
    
    competency_scores: List[CompetencyScore] = Field(default_factory=list)
    technical_score: float = Field(ge=0.0, le=10.0)
    
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    
    evidence: List[EvidenceItem] = Field(default_factory=list)
    explanation: str
    
    confidence: float = Field(ge=0.0, le=1.0)
    requires_human_review: bool = False
    review_reason: Optional[str] = None


class BehavioralEvaluation(BaseModel):
    """Behavioral and soft skills evaluation."""
    evaluation_id: str
    candidate_id: str
    job_id: str
    interview_id: str
    
    competency_scores: List[CompetencyScore] = Field(default_factory=list)
    behavioral_score: float = Field(ge=0.0, le=10.0)
    
    communication: float = Field(ge=0.0, le=10.0)
    problem_solving: float = Field(ge=0.0, le=10.0)
    teamwork: float = Field(ge=0.0, le=10.0)
    adaptability: float = Field(ge=0.0, le=10.0)
    
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    
    evidence: List[EvidenceItem] = Field(default_factory=list)
    explanation: str
    
    confidence: float = Field(ge=0.0, le=1.0)
    requires_human_review: bool = False
    review_reason: Optional[str] = None


class ResumeClaim(BaseModel):
    """Resume claim being verified."""
    claim_id: str
    resume_claim: str
    source: str  # Where in resume


class ClaimVerification(BaseModel):
    """Resume claim verification result."""
    verification_id: str
    candidate_id: str
    job_id: str
    interview_id: str
    
    claim: ResumeClaim
    verification_status: str  # "supported", "partially_supported", "inconsistent", "insufficient_evidence", "requires_human_review"
    confidence: float = Field(ge=0.0, le=1.0)
    
    evidence: List[EvidenceItem] = Field(default_factory=list)
    explanation: str
    
    requires_human_review: bool = False
    review_reason: Optional[str] = None


class IntegrityFlag(BaseModel):
    """Potential integrity concern."""
    flag_id: str
    flag_type: str  # "answer_inconsistency", "resume_interview_mismatch", "suspicious_claim"
    severity: str  # "low", "medium", "high"
    confidence: float = Field(ge=0.0, le=1.0)
    
    evidence: List[EvidenceItem] = Field(default_factory=list)
    description: str
    
    requires_human_review: bool = True


class IntegrityEvaluation(BaseModel):
    """Integrity and consistency analysis."""
    evaluation_id: str
    candidate_id: str
    job_id: str
    interview_id: str
    
    flags: List[IntegrityFlag] = Field(default_factory=list)
    
    overall_integrity: str  # "clear", "flagged", "requires_review"
    explanation: str
    
    confidence: float = Field(ge=0.0, le=1.0)
    requires_human_review: bool = False


class BiasFlag(BaseModel):
    """Potential bias in evaluation."""
    flag_id: str
    bias_type: str  # "demographic", "accent", "appearance", "personality_assumption", "other"
    severity: str  # "low", "medium", "high"
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: List[EvidenceItem] = Field(default_factory=list)
    description: str
    recommendation: str


class BiasAudit(BaseModel):
    """Bias and fairness audit."""
    audit_id: str
    candidate_id: str
    job_id: str
    interview_id: Optional[str] = None

    flags: List[BiasFlag] = Field(default_factory=list)
    fairness_status: str  # "pass", "flagged", "requires_review"

    explanation: str
    confidence: float = Field(ge=0.0, le=1.0)
    requires_human_review: bool = False


class MatchingScore(BaseModel):
    """Resume to job matching score, computed against one rubric version.

    Carries `coverage` and `rubric_version` as first-class fields because a
    leaderboard is only coherent when every row was scored against the same
    rubric, and because a score computed from half the rubric is a different
    kind of claim than one computed from all of it.

    Deliberately carries NO shortlist_recommendation: the matcher ranks and
    explains, it does not decide. Advancing and rejecting are both recruiter
    actions taken after reading the leaderboard.

    `match_score` is None when coverage was too thin to rank the candidate
    fairly - an unknown, not a zero.
    """
    match_id: str
    candidate_id: str
    job_id: str

    # Defaults exist for directly-constructed scores (fixtures, and callers
    # that only need a MatchingScore object). The matcher itself always sets
    # both explicitly, so a real score never relies on these.
    rubric_version: int = 1
    match_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    band: Optional[str] = None

    competency_verdicts: List[CompetencyVerdict] = Field(default_factory=list)
    needs_human_review: bool = False

    explanation: str
    confidence: float = Field(ge=0.0, le=1.0)
