"""
Interview Report API endpoints.

Provides endpoints to generate and retrieve comprehensive interview reports.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional, Dict, Any

from services.interview_report import InterviewReportGenerator
from services.background_scoring import BackgroundScoringService
from core.config import AppSettings
from core.dependencies import get_settings

router = APIRouter(prefix="/reports", tags=["reports"])

# Global report generator instance
_report_generator = InterviewReportGenerator()
_scoring_service = BackgroundScoringService()


def get_report_generator() -> InterviewReportGenerator:
    """Get the global report generator."""
    return _report_generator


def get_scoring_service() -> BackgroundScoringService:
    """Get the global scoring service."""
    return _scoring_service


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
    settings: AppSettings = Depends(get_settings),
) -> Dict[str, Any]:
    """Generate a comprehensive interview report.

    Takes accumulated scores and generates a detailed evaluation report
    with scoring breakdown, feedback, and hiring recommendations.
    """
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
) -> Optional[Dict[str, Any]]:
    """Retrieve a generated interview report."""
    report = generator.get_report(session_id)
    if not report:
        return None
    return report.to_dict()


@router.get("/list")
async def list_reports(
    generator: InterviewReportGenerator = Depends(get_report_generator),
) -> Dict[str, Any]:
    """List all generated reports."""
    report_ids = generator.list_reports()
    return {
        "total": len(report_ids),
        "reports": report_ids,
    }


@router.post("/export/{session_id}")
async def export_report(
    session_id: str,
    format: str = "json",
    generator: InterviewReportGenerator = Depends(get_report_generator),
) -> Dict[str, Any]:
    """Export a report in specified format.

    Supports: json, pdf (pdf requires additional processing)
    """
    report = generator.get_report(session_id)
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
