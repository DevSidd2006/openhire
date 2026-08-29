"""
Interview Mediator API endpoints.

Provides real-time LLM-based guidance during interviews:
- Response analysis and scoring
- Intelligent follow-up question generation
- Coaching and encouragement
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

from agents.interview_mediator import InterviewMediatorAgent
from core.config import AppSettings
from core.dependencies import get_settings
from core.security import Principal, require_authenticated

router = APIRouter(prefix="/mediator", tags=["interview-mediator"])


class AnalyzeResponseRequest(BaseModel):
    """Request to analyze a candidate response."""
    candidate_name: str
    question: str
    answer: str
    competency: str
    context: Optional[str] = None


class GenerateFollowupRequest(BaseModel):
    """Request to generate a follow-up question."""
    question_history: List[Dict[str, str]]
    competency: str
    difficulty_level: str = Field(default="medium", pattern="^(easy|medium|hard)$")
    interview_stage: str = Field(default="mid", pattern="^(opening|mid|closing)$")


class CoachingRequest(BaseModel):
    """Request for coaching guidance."""
    candidate_name: str
    response_quality: int = Field(ge=1, le=10)
    topic: str
    needs_improvement: bool = False


@router.post("/analyze")
async def analyze_response(
    request: AnalyzeResponseRequest,
    settings: AppSettings = Depends(get_settings),
) -> Dict[str, Any]:
    """Analyze a candidate's response to an interview question.

    Returns scoring (clarity, completeness, relevance), strengths,
    areas for improvement, and suggested follow-up question.
    """
    agent = InterviewMediatorAgent()
    result = await agent.analyze_response(
        candidate_name=request.candidate_name,
        question=request.question,
        answer=request.answer,
        competency=request.competency,
        interview_context=request.context,
    )
    return {
        "analysis": result.dict(),
        "timestamp": "now",
    }


@router.post("/followup")
async def generate_followup(
    request: GenerateFollowupRequest,
    settings: AppSettings = Depends(get_settings),
) -> Dict[str, str]:
    """Generate an intelligent follow-up question based on conversation history.

    The question adapts to difficulty level and interview stage.
    """
    agent = InterviewMediatorAgent()
    question = await agent.generate_followup_question(
        question_history=request.question_history,
        competency=request.competency,
        difficulty_level=request.difficulty_level,
        interview_stage=request.interview_stage,
    )
    return {
        "followup_question": question,
        "competency": request.competency,
        "difficulty": request.difficulty_level,
    }


@router.post("/coaching")
async def provide_coaching(
    request: CoachingRequest,
    settings: AppSettings = Depends(get_settings),
) -> Dict[str, Any]:
    """Provide real-time coaching and encouragement to the candidate.

    Includes guidance text and suggestions for improvement.
    """
    agent = InterviewMediatorAgent()
    guidance = await agent.provide_coaching(
        candidate_name=request.candidate_name,
        response_quality=request.response_quality,
        topic=request.topic,
        needs_improvement=request.needs_improvement,
    )
    return {
        "coaching": guidance.dict(),
        "timestamp": "now",
    }
