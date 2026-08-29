"""
Scoring API endpoints - Real-time scoring during interviews.

Provides endpoints to score answers and retrieve accumulated scores
and summaries during and after interviews.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any

from services.background_scoring import BackgroundScoringService, AnswerScore
from core.config import AppSettings
from core.dependencies import get_settings

router = APIRouter(prefix="/scoring", tags=["scoring"])

# Global scoring service instance
_scoring_service = BackgroundScoringService()


def get_scoring_service() -> BackgroundScoringService:
    """Get the global scoring service."""
    return _scoring_service


class InitializeScoringRequest(BaseModel):
    """Request to initialize scoring for a session."""
    session_id: str
    candidate_id: str
    job_id: str


class ScoreAnswerRequest(BaseModel):
    """Request to score a single answer."""
    session_id: str
    question_id: str
    question_text: str
    answer_text: str
    competency: str
    candidate_name: str


class ScoringResponse(BaseModel):
    """Response with scoring summary."""
    clarity: int
    completeness: int
    relevance: int
    total_score: float
    timestamp: str


@router.post("/initialize")
async def initialize_scoring(
    request: InitializeScoringRequest,
    service: BackgroundScoringService = Depends(get_scoring_service),
) -> Dict[str, str]:
    """Initialize scoring for a new interview session."""
    scoring = await service.initialize_session(
        session_id=request.session_id,
        candidate_id=request.candidate_id,
        job_id=request.job_id,
    )
    return {
        "session_id": scoring.session_id,
        "status": "initialized",
        "message": "Scoring initialized for session",
    }


@router.post("/score-answer")
async def score_answer(
    request: ScoreAnswerRequest,
    service: BackgroundScoringService = Depends(get_scoring_service),
) -> Dict[str, Any]:
    """Score a candidate's answer in real-time."""
    score = await service.score_answer(
        session_id=request.session_id,
        question_id=request.question_id,
        question_text=request.question_text,
        answer_text=request.answer_text,
        competency=request.competency,
        candidate_name=request.candidate_name,
    )
    return {
        "score": {
            "clarity": score.clarity,
            "completeness": score.completeness,
            "relevance": score.relevance,
            "competency": score.competency,
            "timestamp": score.timestamp,
        },
        "average": round((score.clarity + score.completeness + score.relevance) / 30, 2),
    }


@router.get("/session/{session_id}/summary")
async def get_session_summary(
    session_id: str,
    service: BackgroundScoringService = Depends(get_scoring_service),
) -> Optional[Dict[str, Any]]:
    """Get scoring summary for a session."""
    return await service.get_session_summary(session_id)


@router.get("/session/{session_id}/scores")
async def get_session_scores(
    session_id: str,
    service: BackgroundScoringService = Depends(get_scoring_service),
) -> Optional[Dict[str, Any]]:
    """Get accumulated scores for a session."""
    scoring = await service.get_session_scores(session_id)
    if not scoring:
        return None
    return {
        "session_id": scoring.session_id,
        "candidate_id": scoring.candidate_id,
        "job_id": scoring.job_id,
        "total_answers": len(scoring.answers),
        "average_score": round(scoring.get_average_score() * 100, 1),
        "competency_scores": {
            k: round(v * 100, 1)
            for k, v in scoring.get_competency_scores().items()
        },
    }


@router.post("/session/{session_id}/finalize")
async def finalize_scoring(
    session_id: str,
    service: BackgroundScoringService = Depends(get_scoring_service),
) -> Optional[Dict[str, Any]]:
    """Finalize scoring when interview is complete."""
    return await service.finalize_scoring(session_id)


@router.delete("/session/{session_id}/clear")
async def clear_session_scores(
    session_id: str,
    service: BackgroundScoringService = Depends(get_scoring_service),
) -> Dict[str, str]:
    """Clear scores for a session."""
    service.clear_session(session_id)
    return {
        "session_id": session_id,
        "status": "cleared",
        "message": "Scores cleared for session",
    }
