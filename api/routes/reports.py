"""
Interview Report API endpoints.

Provides endpoints to generate and retrieve comprehensive interview reports.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional, Dict, Any

from core.config import AppSettings
from core.dependencies import (
    get_app_settings,
    get_candidate_repository,
    get_report_generator,
    get_scoring_service,
)
from core.errors import ForbiddenError
from core.security import Principal, PrincipalType, require_authenticated
from repositories.interfaces import CandidateRepository
from services.interview_report import InterviewReport, InterviewReportGenerator
from services.background_scoring import BackgroundScoringService

router = APIRouter(prefix="/reports", tags=["reports"])

_ELEVATED_REPORT_ROLES = {
    PrincipalType.RECRUITER,
    PrincipalType.ADMIN,
    PrincipalType.SERVICE,
}


async def _can_access_session(
    session_id: str,
    *,
    principal: Principal,
    settings: AppSettings,
    scoring_service: BackgroundScoringService,
    candidate_repository: CandidateRepository,
) -> bool:
    """Whether the caller may read or generate this session's report.

    Auth-disabled development keeps the historical capability model: holding
    the session id is enough. Once auth is enabled, a candidate needs either
    a principal scoped to this exact session or an owned candidate record;
    recruiters/admins may review any session.
    """
    if not settings.auth_enabled:
        return True
    if principal.session_id is not None:
        return principal.session_id == session_id
    if principal.principal_type in _ELEVATED_REPORT_ROLES:
        return True
    if principal.principal_type is not PrincipalType.CANDIDATE:
        return False

    scoring = await scoring_service.get_session_scores(session_id)
    if scoring is None:
        return False
    candidate = await candidate_repository.get(scoring.candidate_id)
    return (
        candidate is not None
        and principal.subject_id is not None
        and candidate.user_id == principal.subject_id
    )


async def _require_session_access(
    session_id: str,
    *,
    principal: Principal,
    settings: AppSettings,
    scoring_service: BackgroundScoringService,
    candidate_repository: CandidateRepository,
) -> None:
    if not await _can_access_session(
        session_id,
        principal=principal,
        settings=settings,
        scoring_service=scoring_service,
        candidate_repository=candidate_repository,
    ):
        raise ForbiddenError(
            "You do not have access to this interview report.",
            internal_detail=(
                f"principal subject_id={principal.subject_id!r} cannot access "
                f"report session_id={session_id!r}"
            ),
            context={"session_id": session_id},
        )


async def _get_fresh_report(
    session_id: str,
    *,
    generator: InterviewReportGenerator,
    scoring_service: BackgroundScoringService,
) -> Optional[InterviewReport]:
    """Return the stored report, regenerating it if answers arrived later.

    A report generated while a score-answer request was still in flight
    snapshots fewer answers than the session now holds. Regenerating on read
    keeps retrieval consistent without requiring the scoring service to know
    about report storage.
    """
    report = generator.get_report(session_id)
    if report is None:
        return None
    scoring = await scoring_service.get_session_scores(session_id)
    if scoring is not None and report.total_answers != len(scoring.answers):
        report = await generator.generate_report(
            session_id=session_id,
            candidate_name=report.candidate_name,
            job_title=report.job_title,
            scoring=scoring,
        )
    return report


class GenerateReportRequest(BaseModel):
    """Request to generate an interview report."""
    session_id: str
    candidate_name: str
    job_title: str
    job_description: Optional[str] = None


@router.post("/generate")
async def generate_report(
    request: GenerateReportRequest,
    generator: InterviewReportGenerator = Depends(get_report_generator),
    scoring_service: BackgroundScoringService = Depends(get_scoring_service),
    candidate_repository: CandidateRepository = Depends(get_candidate_repository),
    principal: Principal = Depends(require_authenticated),
    settings: AppSettings = Depends(get_app_settings),
) -> Dict[str, Any]:
    """Generate a comprehensive interview report.

    Takes accumulated scores and generates a detailed evaluation report
    with scoring breakdown, feedback, and hiring recommendations.
    """
    await _require_session_access(
        request.session_id,
        principal=principal,
        settings=settings,
        scoring_service=scoring_service,
        candidate_repository=candidate_repository,
    )

    # Get accumulated scores
    scoring = await scoring_service.get_session_scores(request.session_id)
    if not scoring:
        return {
            "error": "Session not found or no scores available",
            "session_id": request.session_id,
        }

    # Generate report
    report = await generator.generate_report(
        session_id=request.session_id,
        candidate_name=request.candidate_name,
        job_title=request.job_title,
        scoring=scoring,
        job_description=request.job_description,
    )

    return {
        "status": "generated",
        "report": report.to_dict(),
    }


@router.get("/session/{session_id}")
async def get_report(
    session_id: str,
    generator: InterviewReportGenerator = Depends(get_report_generator),
    scoring_service: BackgroundScoringService = Depends(get_scoring_service),
    candidate_repository: CandidateRepository = Depends(get_candidate_repository),
    principal: Principal = Depends(require_authenticated),
    settings: AppSettings = Depends(get_app_settings),
) -> Optional[Dict[str, Any]]:
    """Retrieve a generated interview report."""
    await _require_session_access(
        session_id,
        principal=principal,
        settings=settings,
        scoring_service=scoring_service,
        candidate_repository=candidate_repository,
    )
    report = await _get_fresh_report(
        session_id, generator=generator, scoring_service=scoring_service
    )
    if not report:
        return None
    return report.to_dict()


@router.get("/list")
async def list_reports(
    generator: InterviewReportGenerator = Depends(get_report_generator),
    scoring_service: BackgroundScoringService = Depends(get_scoring_service),
    candidate_repository: CandidateRepository = Depends(get_candidate_repository),
    principal: Principal = Depends(require_authenticated),
    settings: AppSettings = Depends(get_app_settings),
) -> Dict[str, Any]:
    """List the generated reports visible to the caller."""
    visible_report_ids = [
        session_id
        for session_id in generator.list_reports()
        if await _can_access_session(
            session_id,
            principal=principal,
            settings=settings,
            scoring_service=scoring_service,
            candidate_repository=candidate_repository,
        )
    ]
    return {
        "total": len(visible_report_ids),
        "reports": visible_report_ids,
    }


@router.post("/export/{session_id}")
async def export_report(
    session_id: str,
    format: str = "json",
    generator: InterviewReportGenerator = Depends(get_report_generator),
    scoring_service: BackgroundScoringService = Depends(get_scoring_service),
    candidate_repository: CandidateRepository = Depends(get_candidate_repository),
    principal: Principal = Depends(require_authenticated),
    settings: AppSettings = Depends(get_app_settings),
) -> Dict[str, Any]:
    """Export a report in specified format.

    Supports: json, pdf (pdf requires additional processing)
    """
    await _require_session_access(
        session_id,
        principal=principal,
        settings=settings,
        scoring_service=scoring_service,
        candidate_repository=candidate_repository,
    )
    report = await _get_fresh_report(
        session_id, generator=generator, scoring_service=scoring_service
    )
    if not report:
        return {"error": "Report not found", "session_id": session_id}

    if format == "json":
        return {
            "format": "json",
            "data": report.to_dict(),
        }
    elif format == "pdf":
        return {
            "format": "pdf",
            "message": "PDF export available via PDF service",
            "data": report.to_dict(),
        }
    else:
        return {
            "error": f"Unsupported format: {format}",
            "supported_formats": ["json", "pdf"],
        }
