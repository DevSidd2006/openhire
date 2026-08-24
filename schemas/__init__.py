"""
Pydantic schemas for all data structures.
"""
from schemas.job import JobDescription, Competency, Requirement
from schemas.resume import ParsedResume, Education, WorkExperience, Project, Certification, ResumeClaim
from schemas.interview import InterviewQuestion, InterviewAnswer, InterviewTranscript, InterviewState
from schemas.evaluation import (
    EvidenceItem,
    CompetencyScore,
    TechnicalEvaluation,
    BehavioralEvaluation,
    ClaimVerification,
    IntegrityEvaluation,
    BiasAudit,
    MatchingScore,
)
from schemas.scoring import CandidateScores, CandidateReport, CandidateLeaderboard, LeaderboardEntry
from schemas.audit import AuditLog, PipelineRun

__all__ = [
    # Job
    "JobDescription",
    "Competency",
    "Requirement",
    # Resume
    "ParsedResume",
    "Education",
    "WorkExperience",
    "Project",
    "Certification",
    "ResumeClaim",
    # Interview
    "InterviewQuestion",
    "InterviewAnswer",
    "InterviewTranscript",
    "InterviewState",
    # Evaluation
    "EvidenceItem",
    "CompetencyScore",
    "TechnicalEvaluation",
    "BehavioralEvaluation",
    "ClaimVerification",
    "IntegrityEvaluation",
    "BiasAudit",
    "MatchingScore",
    # Scoring
    "CandidateScores",
    "CandidateReport",
    "CandidateLeaderboard",
    "LeaderboardEntry",
    # Audit
    "AuditLog",
    "PipelineRun",
]
