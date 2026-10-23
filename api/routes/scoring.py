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
    get_app_settings,
    get_candidate_repository,
    get_session_repository,
)
from core.errors import ConflictError, NotFoundError
from core.security import Principal, PrincipalType, require_authenticated
from repositories.interfaces import CandidateRepository, SessionRecord, SessionRepository

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


def _hidden_session(session_id: str, reason: str) -> NotFoundError:
    """Give missing and inaccessible sessions the same public response."""
    return NotFoundError(
        "No interview session exists for the given session_id.",
        internal_detail=reason,
        context={"session_id": session_id},
    )


async def _authorize_scoring_session(
    *,
    session_id: str,
    principal: Principal,
    settings: AppSettings,
    sessions: SessionRepository,
    candidates: CandidateRepository,
) -> SessionRecord:
    """Require access to the interview before touching its scoring state.

    Legacy sessions without an application or registered candidate have no
    persisted owner. Preserve their existing session-ID access for an
    authenticated candidate; application-linked sessions must have an owner.
    """
    session = await sessions.get(session_id)
    if session is None:
        raise _hidden_session(
            session_id, f"interview session missing for session_id={session_id!r}"
        )

    if not settings.auth_enabled:
        return session
    if principal.session_id is not None:
        if principal.session_id == session_id:
            return session
        raise _hidden_session(
            session_id,
            (
                f"session-scoped principal is for {principal.session_id!r}, "
                f"not {session_id!r}"
            ),
        )
    if principal.principal_type in _ELEVATED_SCORING_ROLES:
        return session
    if principal.principal_type is not PrincipalType.CANDIDATE or principal.subject_id is None:
        raise _hidden_session(
            session_id,
            (
                f"principal subject_id={principal.subject_id!r} has "
                f"principal_type={principal.principal_type.value!r}"
            ),
        )
    candidate = await candidates.get(session.candidate_id)
    if candidate is not None and candidate.user_id == principal.subject_id:
        return session
    if candidate is None and session.application_id is None:
        return session
    raise _hidden_session(
        session_id,
        (
            f"principal subject_id={principal.subject_id!r} does not own "
            f"candidate {session.candidate_id!r}"
        ),
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
    settings: AppSettings = Depends(get_app_settings),
) -> Dict[str, str]:
    """Initialize scoring for a new interview session.

    The session must already exist and the supplied candidate/job must match
    its recorded pair. With auth enabled, registered candidates must own the
    session; legacy unregistered sessions keep their existing access policy.
    """
    session = await _authorize_scoring_session(
        session_id=request.session_id,
        principal=principal,
        settings=settings,
        sessions=sessions,
        candidates=candidates,
    )
    if session.candidate_id != request.candidate_id or session.job_id != request.job_id:
        raise ConflictError(
            "Scoring must match the interview session's candidate and job.",
            internal_detail=(
                f"session {request.session_id!r} belongs to candidate "
                f"{session.candidate_id!r} / job {session.job_id!r}, not "
                f"candidate {request.candidate_id!r} / job {request.job_id!r}"
            ),
            context={"session_id": request.session_id},
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
    sessions: SessionRepository = Depends(get_session_repository),
    candidates: CandidateRepository = Depends(get_candidate_repository),
    principal: Principal = Depends(require_authenticated),
    settings: AppSettings = Depends(get_app_settings),
) -> Dict[str, Any]:
    """Score a candidate's answer in real-time."""
    await _authorize_scoring_session(
        session_id=request.session_id, principal=principal, settings=settings,
        sessions=sessions, candidates=candidates,
    )
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
    sessions: SessionRepository = Depends(get_session_repository),
    candidates: CandidateRepository = Depends(get_candidate_repository),
    principal: Principal = Depends(require_authenticated),
    settings: AppSettings = Depends(get_app_settings),
) -> Optional[Dict[str, Any]]:
    """Get scoring summary for a session."""
    await _authorize_scoring_session(
        session_id=session_id, principal=principal, settings=settings,
        sessions=sessions, candidates=candidates,
    )
    return await service.get_session_summary(session_id)


@router.get("/session/{session_id}/scores")
async def get_session_scores(
    session_id: str,
    service: BackgroundScoringService = Depends(get_scoring_service),
    sessions: SessionRepository = Depends(get_session_repository),
    candidates: CandidateRepository = Depends(get_candidate_repository),
    principal: Principal = Depends(require_authenticated),
    settings: AppSettings = Depends(get_app_settings),
) -> Optional[Dict[str, Any]]:
    """Get accumulated scores for a session."""
    await _authorize_scoring_session(
        session_id=session_id, principal=principal, settings=settings,
        sessions=sessions, candidates=candidates,
    )
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
    sessions: SessionRepository = Depends(get_session_repository),
    candidates: CandidateRepository = Depends(get_candidate_repository),
    principal: Principal = Depends(require_authenticated),
    settings: AppSettings = Depends(get_app_settings),
) -> Optional[Dict[str, Any]]:
    """Finalize scoring when interview is complete."""
    await _authorize_scoring_session(
        session_id=session_id, principal=principal, settings=settings,
        sessions=sessions, candidates=candidates,
    )
    return await service.finalize_scoring(session_id)


@router.delete("/session/{session_id}/clear")
async def clear_session_scores(
    session_id: str,
    service: BackgroundScoringService = Depends(get_scoring_service),
    sessions: SessionRepository = Depends(get_session_repository),
    candidates: CandidateRepository = Depends(get_candidate_repository),
    principal: Principal = Depends(require_authenticated),
    settings: AppSettings = Depends(get_app_settings),
) -> Dict[str, str]:
    """Clear scores for a session."""
    await _authorize_scoring_session(
        session_id=session_id, principal=principal, settings=settings,
        sessions=sessions, candidates=candidates,
    )
    service.clear_session(session_id)
    return {
        "session_id": session_id,
        "status": "cleared",
        "message": "Scores cleared for session",
    }
