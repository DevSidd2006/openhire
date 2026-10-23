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
from core.dependencies import (
    get_candidate_repository,
    get_session_repository,
    get_settings,
)
from core.errors import ConflictError, ForbiddenError, NotFoundError
from core.security import Principal, PrincipalType, require_authenticated
from repositories.interfaces import CandidateRepository, SessionRepository

router = APIRouter(prefix="/scoring", tags=["scoring"])

# Global scoring service instance
_scoring_service = BackgroundScoringService()


def get_scoring_service() -> BackgroundScoringService:
    """Get the global scoring service."""
    return _scoring_service


_ELEVATED_SCORING_ROLES = {
    PrincipalType.RECRUITER,
    PrincipalType.ADMIN,
    PrincipalType.SERVICE,
}


async def _authorize_initialization(
    *,
    session_id: str,
    candidate_id: str,
    job_id: str,
    principal: Principal,
    settings: AppSettings,
    sessions: SessionRepository,
    candidates: CandidateRepository,
) -> None:
    """Tie scoring to an existing interview before accepting its ids.

    Possession of a session id is not an authorization check. Every scoring
    session must match a real interview record; when auth is enabled the
    caller must own that interview's candidate (or be a recruiter/admin, or
    hold a principal scoped to this exact session).
    """
    session = await sessions.get(session_id)
    if session is None:
        raise NotFoundError(
            "No interview session exists for the given session_id.",
            internal_detail=f"interview session missing for session_id={session_id!r}",
            context={"session_id": session_id},
        )
    if session.candidate_id != candidate_id or session.job_id != job_id:
        raise ConflictError(
            "Scoring must match the interview session's candidate and job.",
            internal_detail=(
                f"session {session_id!r} belongs to candidate "
                f"{session.candidate_id!r} / job {session.job_id!r}, not "
                f"candidate {candidate_id!r} / job {job_id!r}"
            ),
            context={"session_id": session_id},
        )

    if not settings.auth_enabled:
        return
    if principal.session_id is not None:
        if principal.session_id == session_id:
            return
        raise ForbiddenError(
            "You do not have access to this interview session.",
            internal_detail=(
                f"session-scoped principal is for {principal.session_id!r}, "
                f"not {session_id!r}"
            ),
            context={"session_id": session_id},
        )
    if principal.principal_type in _ELEVATED_SCORING_ROLES:
        return
    if principal.principal_type is not PrincipalType.CANDIDATE:
        raise ForbiddenError(
            "You do not have access to this interview session.",
            internal_detail=(
                f"principal subject_id={principal.subject_id!r} has "
                f"principal_type={principal.principal_type.value!r}"
            ),
            context={"session_id": session_id},
        )
    candidate = await candidates.get(candidate_id)
    if (
        candidate is None
        or principal.subject_id is None
        or candidate.user_id != principal.subject_id
    ):
        raise ForbiddenError(
            "You do not have access to this interview session.",
            internal_detail=(
                f"principal subject_id={principal.subject_id!r} does not own "
                f"candidate {candidate_id!r}"
            ),
            context={"session_id": session_id},
        )


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


@router.post(
    "/initialize",
    responses={
        404: {"description": "No interview session exists for the session_id."},
        409: {"description": "The candidate/job does not match the session."},
    },
)
async def initialize_scoring(
    request: InitializeScoringRequest,
    service: BackgroundScoringService = Depends(get_scoring_service),
    sessions: SessionRepository = Depends(get_session_repository),
    candidates: CandidateRepository = Depends(get_candidate_repository),
    principal: Principal = Depends(require_authenticated),
    settings: AppSettings = Depends(get_settings),
) -> Dict[str, str]:
    """Initialize scoring for a new interview session.

    The session must already exist and the supplied candidate/job must match
    its recorded pair. With auth enabled, the caller must own the candidate,
    hold a session-scoped principal, or be a recruiter/admin.
    """
    await _authorize_initialization(
        session_id=request.session_id,
        candidate_id=request.candidate_id,
        job_id=request.job_id,
        principal=principal,
        settings=settings,
        sessions=sessions,
        candidates=candidates,
    )
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
