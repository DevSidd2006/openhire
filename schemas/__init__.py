"""
Pydantic schemas for all data structures.
"""
from schemas.job import JobDescription, Competency, Requirement
from schemas.resume import ParsedResume, Education, WorkExperience, Project, Certification, ResumeClaim
from schemas.interview import (
    InterviewQuestion,
    InterviewAnswer,
    InterviewTranscript,
    InterviewState,
    QuestionType,
    QuestionAction,
    CompetencySignal,
    NextQuestionDecision,
)
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
from schemas.llm_outputs import (
    CompetencyJudgment,
    TechnicalEvaluationResult,
    BehavioralEvaluationResult,
    ClaimVerificationResult,
    IntegrityFlagResult,
    IntegrityCheckResult,
    BiasFlagResult,
    BiasCheckResult,
    InterviewQuestionResult,
    RawCompetency,
    JDAnalysisResult,
    AnswerEvaluationResult,
    AdaptiveQuestionResult,
    ResumeEducationExtract,
    ResumeWorkExperienceExtract,
    ResumeProjectExtract,
    ResumeCertificationExtract,
    ResumeParseResult,
)

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
    "QuestionType",
    "QuestionAction",
    "CompetencySignal",
    "NextQuestionDecision",
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
    # LLM output contracts (P1)
    "CompetencyJudgment",
    "TechnicalEvaluationResult",
    "BehavioralEvaluationResult",
    "ClaimVerificationResult",
    "IntegrityFlagResult",
    "IntegrityCheckResult",
    "BiasFlagResult",
    "BiasCheckResult",
    "InterviewQuestionResult",
    "RawCompetency",
    "JDAnalysisResult",
    "AnswerEvaluationResult",
    "AdaptiveQuestionResult",
    "ResumeEducationExtract",
    "ResumeWorkExperienceExtract",
    "ResumeProjectExtract",
    "ResumeCertificationExtract",
    "ResumeParseResult",
]
