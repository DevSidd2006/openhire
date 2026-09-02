"""
Scoring and reporting related schemas.
"""
from typing import List, Optional
from pydantic import BaseModel, Field

from schemas.evaluation import IntegrityFlag, BiasFlag, CompetencyScore, ClaimVerification


class CandidateScores(BaseModel):
    """Complete candidate scores, synthesized by the ScoringAgent."""
    score_id: str
    candidate_id: str
    job_id: str

    # Component scores (0-10 scale)
    technical_score: float = Field(ge=0.0, le=10.0)
    behavioral_score: float = Field(ge=0.0, le=10.0)
    job_fit_score: float = Field(ge=0.0, le=10.0)

    # Weighted final score (0-10 scale)
    weighted_final_score: float = Field(ge=0.0, le=10.0)

    # Per-competency breakdown of the job's own rubric (name, weight, matched
    # score) actually used to compute weighted_final_score - see
    # ScoringAgent._compute_rubric_score for the formula. Empty if no
    # competency in the job's rubric could be matched to an evaluator score.
    competency_scores: List[CompetencyScore] = Field(default_factory=list)

    # Fraction (0-1) of the job's total competency weight that was actually
    # matched to an evaluator score and contributed to competency_scores.
    rubric_coverage: float = Field(default=0.0, ge=0.0, le=1.0)

    # Filled in by the LeaderboardAgent once all candidates are ranked
    percentile_rank: float = Field(default=0.0, ge=0.0, le=100.0)

    explanation: str
    confidence: float = Field(ge=0.0, le=1.0)


class CandidateReport(BaseModel):
    """Comprehensive candidate evaluation report, produced by the ReportGeneratorAgent."""
    report_id: str
    candidate_id: str
    job_id: str
    candidate_name: str

    scores: CandidateScores

    technical_summary: str
    behavioral_summary: str
    job_fit_summary: str

    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)

    integrity_flags: List[IntegrityFlag] = Field(default_factory=list)
    bias_flags: List[BiasFlag] = Field(default_factory=list)

    # Resume-claim verification results (P2 Phase 10 fix): previously the
    # Resume Auditor's ClaimVerification objects - and the evidence attached
    # to them - never reached the final report at all, even though the
    # ScoringAgent and every other evaluator's evidence did. Reusing the
    # existing ClaimVerification schema rather than inventing a parallel one.
    claim_verifications: List[ClaimVerification] = Field(default_factory=list)

    requires_human_review: bool = False

    # Recommendation
    recommendation: str  # "strong_candidate", "candidate", "human_review", "insufficient_evidence"
    explanation: str


class LeaderboardEntry(BaseModel):
    """Single entry in candidate leaderboard, produced by the LeaderboardAgent."""
    entry_id: str
    rank: int
    candidate_id: str
    candidate_name: str

    weighted_score: float
    technical_score: float
    behavioral_score: float
    job_fit_score: float
    percentile_rank: float

    recommendation: str
    has_integrity_issues: bool
    has_bias_concerns: bool
    requires_human_review: bool

    is_selected: bool = Field(default=False, description="Whether this candidate is selected for one of the job openings")
    selection_status: str = Field(default="pending", description="selected, waitlisted, or rejected")


class CandidateLeaderboard(BaseModel):
    """Ranked candidate leaderboard, produced by the LeaderboardAgent."""
    leaderboard_id: str
    job_id: str
    openings: int = Field(default=1, description="Number of job openings/vacancies")

    total_candidates: int
    strong_candidates: int
    candidates: int
    requires_review: int

    entries: List[LeaderboardEntry] = Field(default_factory=list)
    top_candidates: List[LeaderboardEntry] = Field(default_factory=list)
    selected_candidates: List[LeaderboardEntry] = Field(default_factory=list)
    selected_candidate_ids: List[str] = Field(default_factory=list)

    # Candidate IDs that were shortlisted but never produced a scored report
    # (e.g. a technical/behavioral evaluation failed or was never returned).
    # They are deliberately excluded from `entries` rather than silently
    # scored as if evaluation had succeeded - see ScoringAgent and
    # orchestration/graph.py node_score_candidates.
    incomplete_candidates: List[str] = Field(default_factory=list)

    explanation: str
