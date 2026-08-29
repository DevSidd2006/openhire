"""
Interview Report Generation Service - Comprehensive evaluation reports.

Generates detailed interview reports with:
- Scoring breakdown by competency
- Qualitative feedback and insights
- Strengths and areas for improvement
- Hiring recommendations
"""
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from services.background_scoring import InterviewScoring, AnswerScore
from agents.interview_mediator import InterviewMediatorAgent
from utils.logging import get_logger

logger = get_logger("interview_report")


class RecommendationLevel(str, Enum):
    """Hiring recommendation levels."""
    STRONG_YES = "strong_yes"
    YES = "yes"
    MAYBE = "maybe"
    NO = "no"
    STRONG_NO = "strong_no"


@dataclass
class CompetencyFeedback:
    """Feedback for a specific competency."""
    competency: str
    score: float  # 0-1
    evidence: List[str]
    strengths: List[str]
    improvements: List[str]
    examples: List[str] = field(default_factory=list)


@dataclass
class InterviewReport:
    """Comprehensive interview evaluation report."""
    session_id: str
    candidate_name: str
    job_title: str
    interview_date: str
    duration_minutes: int

    # Scores
    overall_score: float  # 0-1
    competency_scores: Dict[str, float]

    # Feedback
    strengths: List[str]
    areas_for_improvement: List[str]
    competency_feedback: List[CompetencyFeedback]

    # Recommendation
    recommendation: RecommendationLevel
    recommendation_rationale: str

    # Details
    total_answers: int
    average_clarity: float
    average_completeness: float
    average_relevance: float

    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        """Convert report to dictionary."""
        return {
            "session_id": self.session_id,
            "candidate_name": self.candidate_name,
            "job_title": self.job_title,
            "interview_date": self.interview_date,
            "duration_minutes": self.duration_minutes,
            "overall_score": round(self.overall_score * 100, 1),
            "competency_scores": {k: round(v * 100, 1) for k, v in self.competency_scores.items()},
            "strengths": self.strengths,
            "areas_for_improvement": self.areas_for_improvement,
            "competency_feedback": [
                {
                    "competency": cf.competency,
                    "score": round(cf.score * 100, 1),
                    "strengths": cf.strengths,
                    "improvements": cf.improvements,
                    "evidence": cf.evidence[:2],  # Top 2 evidence items
                }
                for cf in self.competency_feedback
            ],
            "recommendation": self.recommendation.value,
            "recommendation_rationale": self.recommendation_rationale,
            "total_answers": self.total_answers,
            "average_clarity": round(self.average_clarity, 1),
            "average_completeness": round(self.average_completeness, 1),
            "average_relevance": round(self.average_relevance, 1),
            "generated_at": self.generated_at,
        }


class InterviewReportGenerator:
    """Generates comprehensive interview reports."""

    def __init__(self):
        self.mediator = InterviewMediatorAgent()
        self.reports: Dict[str, InterviewReport] = {}

    async def generate_report(
        self,
        session_id: str,
        candidate_name: str,
        job_title: str,
        scoring: InterviewScoring,
        job_description: Optional[str] = None,
    ) -> InterviewReport:
        """Generate a comprehensive interview report.

        Args:
            session_id: Interview session ID
            candidate_name: Candidate's name
            job_title: Position title
            scoring: Accumulated interview scores
            job_description: Optional job description for context

        Returns:
            Comprehensive InterviewReport
        """
        logger.info(f"Generating report for session {session_id}")

        # Calculate overall metrics
        overall_score = scoring.get_average_score()
        competency_scores = scoring.get_competency_scores()

        # Extract answer metrics
        answer_count = len(scoring.answers)
        avg_clarity = (
            sum(a.clarity for a in scoring.answers) / answer_count
            if answer_count > 0
            else 0
        ) / 10.0
        avg_completeness = (
            sum(a.completeness for a in scoring.answers) / answer_count
            if answer_count > 0
            else 0
        ) / 10.0
        avg_relevance = (
            sum(a.relevance for a in scoring.answers) / answer_count
            if answer_count > 0
            else 0
        ) / 10.0

        # Generate qualitative feedback
        strengths = await self._generate_strengths(candidate_name, scoring.answers)
        improvements = await self._generate_improvements(candidate_name, scoring.answers)
        competency_feedback = await self._generate_competency_feedback(
            candidate_name, scoring.answers
        )

        # Determine recommendation
        recommendation, rationale = self._determine_recommendation(
            overall_score, competency_scores, answer_count
        )

        # Calculate interview duration (estimate based on answer count)
        duration_minutes = max(10, answer_count * 3)

        # Create report
        report = InterviewReport(
            session_id=session_id,
            candidate_name=candidate_name,
            job_title=job_title,
            interview_date=datetime.now().isoformat().split("T")[0],
            duration_minutes=duration_minutes,
            overall_score=overall_score,
            competency_scores=competency_scores,
            strengths=strengths,
            areas_for_improvement=improvements,
            competency_feedback=competency_feedback,
            recommendation=recommendation,
            recommendation_rationale=rationale,
            total_answers=answer_count,
            average_clarity=avg_clarity,
            average_completeness=avg_completeness,
            average_relevance=avg_relevance,
        )

        # Store report
        self.reports[session_id] = report
        logger.info(f"Generated report for session {session_id}")

        return report

    async def _generate_strengths(
        self, candidate_name: str, answers: List[AnswerScore]
    ) -> List[str]:
        """Generate list of candidate strengths."""
        if not answers:
            return []

        high_scoring_answers = [a for a in answers if a.clarity >= 7]
        if not high_scoring_answers:
            return ["Shows engagement in interview process"]

        strengths = [
            "Clear communication and articulation",
            "Strong technical knowledge demonstration",
            "Thoughtful and structured responses",
            "Demonstrates problem-solving ability",
            "Shows enthusiasm and passion for the role",
        ]
        return strengths[:3]  # Return top 3

    async def _generate_improvements(
        self, candidate_name: str, answers: List[AnswerScore]
    ) -> List[str]:
        """Generate areas for improvement."""
        if not answers:
            return ["Provide more detailed examples and evidence"]

        low_scoring_answers = [a for a in answers if a.relevance < 6]
        improvements = []

        if low_scoring_answers:
            improvements.extend([
                "Focus more directly on relevant experience",
                "Provide more specific examples and metrics",
                "Practice concise and structured communication",
            ])

        return improvements[:3]

    async def _generate_competency_feedback(
        self, candidate_name: str, answers: List[AnswerScore]
    ) -> List[CompetencyFeedback]:
        """Generate feedback for each competency."""
        competency_groups: Dict[str, List[AnswerScore]] = {}
        for answer in answers:
            if answer.competency not in competency_groups:
                competency_groups[answer.competency] = []
            competency_groups[answer.competency].append(answer)

        feedback = []
        for competency, comp_answers in competency_groups.items():
            avg_score = (
                sum(a.clarity + a.completeness + a.relevance for a in comp_answers)
                / (len(comp_answers) * 30)
            )

            comp_feedback = CompetencyFeedback(
                competency=competency,
                score=avg_score,
                evidence=[a.question_text for a in comp_answers[:2]],
                strengths=[
                    f"Demonstrated {competency.lower()} capability",
                    f"Strong practical knowledge of {competency.lower()}",
                ],
                improvements=[
                    f"Expand experience in {competency.lower()}",
                    f"Provide more examples of {competency.lower()} success",
                ],
            )
            feedback.append(comp_feedback)

        return feedback

    def _determine_recommendation(
        self,
        overall_score: float,
        competency_scores: Dict[str, float],
        answer_count: int,
    ) -> tuple[RecommendationLevel, str]:
        """Determine hiring recommendation based on scores."""
        if answer_count < 3:
            return (
                RecommendationLevel.MAYBE,
                "Insufficient data from interview. More questions needed.",
            )

        if overall_score >= 0.85:
            return (
                RecommendationLevel.STRONG_YES,
                "Excellent performance across all competencies. Strong hire recommendation.",
            )
        elif overall_score >= 0.70:
            return (
                RecommendationLevel.YES,
                "Good performance with solid competency demonstration. Recommend for next round.",
            )
        elif overall_score >= 0.55:
            return (
                RecommendationLevel.MAYBE,
                "Mixed performance. Some competencies strong, others need development. Consider for specific roles.",
            )
        elif overall_score >= 0.40:
            return (
                RecommendationLevel.NO,
                "Below-average performance on key competencies. Not recommended at this time.",
            )
        else:
            return (
                RecommendationLevel.STRONG_NO,
                "Significant gaps in required competencies. Not a good fit for this role.",
            )

    def get_report(self, session_id: str) -> Optional[InterviewReport]:
        """Retrieve a generated report."""
        return self.reports.get(session_id)

    def list_reports(self) -> List[str]:
        """List all generated report session IDs."""
        return list(self.reports.keys())
