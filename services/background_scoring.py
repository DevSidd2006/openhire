"""
Background Scoring Service - Real-time evaluation during interviews.

Runs evaluation agents in parallel as candidate answers questions,
collecting scores and building a comprehensive report in the background.
"""
import asyncio
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime

from agents.technical_evaluator import TechnicalEvaluatorAgent
from agents.behavioral_evaluator import BehavioralEvaluatorAgent
from agents.interview_mediator import InterviewMediatorAgent
from utils.logging import get_logger

logger = get_logger("background_scoring")


@dataclass
class AnswerScore:
    """Score for a single answer."""
    question_id: str
    question_text: str
    answer_text: str
    competency: str
    clarity: int = 0  # 1-10
    completeness: int = 0  # 1-10
    relevance: int = 0  # 1-10
    technical_score: Optional[float] = None  # 0-1
    behavioral_score: Optional[float] = None  # 0-1
    evidence: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class InterviewScoring:
    """Accumulated scores for an entire interview."""
    session_id: str
    candidate_id: str
    job_id: str
    answers: List[AnswerScore] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def add_answer(self, answer_score: AnswerScore) -> None:
        """Add a scored answer."""
        self.answers.append(answer_score)
        self.updated_at = datetime.now().isoformat()

    def get_average_score(self) -> float:
        """Calculate average score across all answers."""
        if not self.answers:
            return 0.0
        scores = [
            (a.clarity + a.completeness + a.relevance) / 30.0
            for a in self.answers
            if a.clarity > 0
        ]
        return sum(scores) / len(scores) if scores else 0.0

    def get_competency_scores(self) -> Dict[str, float]:
        """Get average score per competency."""
        competency_scores: Dict[str, List[float]] = {}
        for answer in self.answers:
            if answer.competency not in competency_scores:
                competency_scores[answer.competency] = []
            score = (answer.clarity + answer.completeness + answer.relevance) / 30.0
            competency_scores[answer.competency].append(score)

        return {
            competency: sum(scores) / len(scores)
            for competency, scores in competency_scores.items()
        }

    def get_summary(self) -> Dict[str, Any]:
        """Get scoring summary."""
        return {
            "total_answers": len(self.answers),
            "average_score": round(self.get_average_score() * 100, 1),
            "competency_scores": {
                k: round(v * 100, 1)
                for k, v in self.get_competency_scores().items()
            },
            "answers": [
                {
                    "question": a.question_text,
                    "clarity": a.clarity,
                    "completeness": a.completeness,
                    "relevance": a.relevance,
                    "timestamp": a.timestamp,
                }
                for a in self.answers
            ],
        }


class BackgroundScoringService:
    """Manages real-time scoring during interviews."""

    def __init__(self):
        self.scores: Dict[str, InterviewScoring] = {}
        self.mediator_agent = InterviewMediatorAgent()
        self.technical_agent = TechnicalEvaluatorAgent()
        self.behavioral_agent = BehavioralEvaluatorAgent()

    async def initialize_session(
        self, session_id: str, candidate_id: str, job_id: str
    ) -> InterviewScoring:
        """Initialize scoring for a new interview session."""
        scoring = InterviewScoring(
            session_id=session_id,
            candidate_id=candidate_id,
            job_id=job_id,
        )
        self.scores[session_id] = scoring
        logger.info(f"Initialized scoring for session {session_id}")
        return scoring

    async def score_answer(
        self,
        session_id: str,
        question_id: str,
        question_text: str,
        answer_text: str,
        competency: str,
        candidate_name: str,
    ) -> AnswerScore:
        """Score a candidate's answer using LLM analysis.

        Runs mediator analysis to get clarity/completeness/relevance scores.
        """
        if session_id not in self.scores:
            logger.error(f"Session {session_id} not found")
            # Create default score
            return AnswerScore(
                question_id=question_id,
                question_text=question_text,
                answer_text=answer_text,
                competency=competency,
            )

        try:
            # Get LLM analysis
            analysis = await self.mediator_agent.analyze_response(
                candidate_name=candidate_name,
                question=question_text,
                answer=answer_text,
                competency=competency,
            )

            # Create scored answer
            score = AnswerScore(
                question_id=question_id,
                question_text=question_text,
                answer_text=answer_text,
                competency=competency,
                clarity=analysis.clarity,
                completeness=analysis.completeness,
                relevance=analysis.relevance,
                evidence=analysis.areas_for_improvement,
            )

            # Add to session scores
            self.scores[session_id].add_answer(score)
            logger.info(f"Scored answer for session {session_id}")

            return score

        except Exception as e:
            logger.error(f"Error scoring answer: {e}")
            # Return default score on error
            return AnswerScore(
                question_id=question_id,
                question_text=question_text,
                answer_text=answer_text,
                competency=competency,
            )

    async def get_session_scores(self, session_id: str) -> Optional[InterviewScoring]:
        """Get accumulated scores for a session."""
        return self.scores.get(session_id)

    async def get_session_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get scoring summary for a session."""
        scoring = self.scores.get(session_id)
        return scoring.get_summary() if scoring else None

    async def finalize_scoring(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Finalize scoring when interview is complete."""
        scoring = self.scores.get(session_id)
        if not scoring:
            return None

        logger.info(
            f"Finalized scoring for session {session_id}. "
            f"Total answers: {len(scoring.answers)}, "
            f"Average score: {scoring.get_average_score():.2f}"
        )

        return scoring.get_summary()

    def clear_session(self, session_id: str) -> None:
        """Clear scores for a session."""
        if session_id in self.scores:
            del self.scores[session_id]
            logger.info(f"Cleared scores for session {session_id}")
