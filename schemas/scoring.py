"""
Scoring and reporting related schemas.
"""
from typing import List, Optional
from pydantic import BaseModel, Field

from schemas.evaluation import IntegrityFlag, BiasFlag


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


class CandidateLeaderboard(BaseModel):
    """Ranked candidate leaderboard, produced by the LeaderboardAgent."""
    leaderboard_id: str
    job_id: str

    total_candidates: int
    strong_candidates: int
    candidates: int
    requires_review: int

    entries: List[LeaderboardEntry] = Field(default_factory=list)
    top_candidates: List[LeaderboardEntry] = Field(default_factory=list)

    explanation: str
