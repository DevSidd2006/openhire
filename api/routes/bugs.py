"""
OpenBox endpoints: platform-wide, user-reported bug tracking.

Thin translators, same pattern as api/routes/applications.py: no triage
logic lives here - that stays inside services/bug_report_service.py.

Every authenticated user can file a report and read the full feed (OpenBox
is a shared board, not a per-user inbox - there is no ownership check on
GET the way api/routes/candidates.py has). Only a status change is
recruiter-scoped, mirroring how shortlist/reject are gated in
api/routes/applications.py.
"""
from fastapi import APIRouter, Depends

from api.models_bugs import (
    BugReportListResponse,
    BugReportResponse,
    FileBugReportRequest,
    UpdateBugStatusRequest,
)
from core.dependencies import get_bug_report_service
from core.security import Principal, require_authenticated, require_scopes
from services.bug_report_service import BugReportService

router = APIRouter(prefix="/bugs", tags=["openbox"])


@router.post("", response_model=BugReportResponse, status_code=201)
async def file_bug_report(
    payload: FileBugReportRequest,
    service: BugReportService = Depends(get_bug_report_service),
    principal: Principal = Depends(require_authenticated),
) -> BugReportResponse:
    """POST /bugs - file a new bug report against the platform.

    The reporter is always the authenticated caller (principal.subject_id);
    never accepted from the request body. When AUTH_ENABLED=false
    (tests/dev), subject_id is None, so a default test user_id is used -
    the same fallback api/routes/candidates.py already established.
    """
    user_id = principal.subject_id or "user_anonymous"
    record = await service.file_report(
        title=payload.title,
        description=payload.description,
        reporter_user_id=user_id,
        severity=payload.severity,
        page=payload.page,
    )
    return BugReportResponse.from_domain(record)


@router.get("", response_model=BugReportListResponse)
async def list_bug_reports(
    service: BugReportService = Depends(get_bug_report_service),
    principal: Principal = Depends(require_authenticated),
) -> BugReportListResponse:
    """GET /bugs - every report on the platform, newest first. OpenBox is a
    shared, public-within-the-platform board: no per-caller filtering."""
    records = await service.list_reports()
    return BugReportListResponse.from_domain(records)


@router.get("/{bug_id}", response_model=BugReportResponse)
async def get_bug_report(
    bug_id: str,
    service: BugReportService = Depends(get_bug_report_service),
    principal: Principal = Depends(require_authenticated),
) -> BugReportResponse:
    """GET /bugs/{bug_id}. Errors: 404 `not_found`."""
    record = await service.get_report(bug_id)
    return BugReportResponse.from_domain(record)


@router.patch("/{bug_id}/status", response_model=BugReportResponse)
async def update_bug_status(
    bug_id: str,
    payload: UpdateBugStatusRequest,
    service: BugReportService = Depends(get_bug_report_service),
    principal: Principal = Depends(require_scopes("recruiter:write")),
) -> BugReportResponse:
    """PATCH /bugs/{bug_id}/status - triage: move a report through OPEN ->
    IN_PROGRESS -> RESOLVED/WONT_FIX. Recruiter-only, the same scope
    api/routes/applications.py's shortlist/reject actions require.

    Errors: 404 `not_found`. Idempotent - setting a report to the status it
    already has is a no-op success.
    """
    record = await service.update_status(bug_id, payload.status)
    return BugReportResponse.from_domain(record)
