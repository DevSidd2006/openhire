"""
Admin console API routes.

Every route here is gated by `require_admin` (core/security.py), which
403s any authenticated-but-non-admin caller and 401s an unauthenticated
one. Thin translators, same convention as every other router in this
codebase: validate -> call exactly one AdminService method -> shape the
response.

Endpoints (grown across the admin console plan's phases):
  - GET /admin/users: search/filter user accounts
  - PATCH /admin/users/{user_id}: change is_active/user_type
  - GET /admin/jobs: list jobs, optionally including archived/hidden ones
  - PATCH /admin/jobs/{job_id}: hide (archive) or restore a job
  - GET /admin/candidates: list candidates, optionally including hidden ones
  - PATCH /admin/candidates/{candidate_id}: hide or unhide a candidate
  - GET /admin/applications: list applications, optionally including hidden ones
  - PATCH /admin/applications/{application_id}: hide or unhide an application
  - GET /admin/metrics: simple platform counts (users/jobs/applications)
  - GET /admin/system/status: live backend health (persistence, providers,
    a live DB ping, JWT placeholder-secret check, evaluation queue depth)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from core.dependencies import get_admin_service
from core.security import Principal, require_admin
from api.models_applications import ApplicationListResponse, ApplicationResponse
from api.models_candidates import CandidateListResponse, CandidateResponse
from api.models_jobs import JobListResponse, JobResponse
from schemas.admin import (
    AdminMetricsResponse,
    AdminSetHiddenRequest,
    AdminSetJobActiveRequest,
    AdminUpdateUserRequest,
    AdminUserListResponse,
    ImpersonateResponse,
    SystemStatusResponse,
)
from schemas.auth import UserProfileResponse
from services.admin_service import AdminService

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=AdminUserListResponse)
async def list_users(
    query: Optional[str] = Query(default=None),
    user_type: Optional[str] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> AdminUserListResponse:
    """GET /admin/users?query=&user_type=&is_active= - search/filter every
    user account. `query` matches case-insensitively against email/full_name.

    Errors: 401 Unauthorized; 403 forbidden if the caller is not an admin.
    """
    users = await service.list_users(query=query, user_type=user_type, is_active=is_active)
    return AdminUserListResponse.from_records(users)


@router.patch("/users/{user_id}", response_model=UserProfileResponse)
async def update_user(
    user_id: str,
    request: AdminUpdateUserRequest,
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> UserProfileResponse:
    """PATCH /admin/users/{user_id} - change `is_active` and/or `user_type`.

    Errors: 401 Unauthorized; 403 forbidden if the caller is not an admin;
    400 invalid_request if `user_type` is present and not one of
    candidate/recruiter/admin; 404 not_found if no such user exists.
    """
    updates = request.model_dump(exclude_unset=True)
    user = await service.update_user(user_id, updates)
    return UserProfileResponse.from_record(user)


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    include_hidden: bool = Query(default=False),
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> JobListResponse:
    """GET /admin/jobs?include_hidden=true - every job, including archived/
    admin-hidden ones when asked for."""
    records = await service.list_jobs(include_hidden=include_hidden)
    return JobListResponse.from_records(records)


@router.patch("/jobs/{job_id}", response_model=JobResponse)
async def set_job_active(
    job_id: str,
    request: AdminSetJobActiveRequest,
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> JobResponse:
    """PATCH /admin/jobs/{job_id} - {"is_active": false} hides the job,
    {"is_active": true} restores it. Errors: 404 not_found."""
    record = await service.set_job_active(job_id, request.is_active)
    return JobResponse.from_record(record)


@router.get("/candidates", response_model=CandidateListResponse)
async def list_candidates(
    include_hidden: bool = Query(default=False),
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> CandidateListResponse:
    """GET /admin/candidates?include_hidden=true - every candidate,
    including admin-hidden ones when asked for."""
    records = await service.list_candidates(include_hidden=include_hidden)
    return CandidateListResponse.from_records(records)


@router.patch("/candidates/{candidate_id}", response_model=CandidateResponse)
async def set_candidate_hidden(
    candidate_id: str,
    request: AdminSetHiddenRequest,
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> CandidateResponse:
    """PATCH /admin/candidates/{candidate_id} - {"is_hidden": bool}.
    Errors: 404 not_found."""
    record = await service.set_candidate_hidden(candidate_id, request.is_hidden)
    return CandidateResponse.from_record(record)


@router.get("/applications", response_model=ApplicationListResponse)
async def list_applications(
    include_hidden: bool = Query(default=False),
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> ApplicationListResponse:
    """GET /admin/applications?include_hidden=true - every application on
    the platform, including admin-hidden ones when asked for."""
    applications = await service.list_applications(include_hidden=include_hidden)
    return ApplicationListResponse.from_domain(applications)


@router.patch("/applications/{application_id}", response_model=ApplicationResponse)
async def set_application_hidden(
    application_id: str,
    request: AdminSetHiddenRequest,
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> ApplicationResponse:
    """PATCH /admin/applications/{application_id} - {"is_hidden": bool}.
    Errors: 404 not_found."""
    application = await service.set_application_hidden(application_id, request.is_hidden)
    return ApplicationResponse.from_domain(application)


@router.get("/metrics", response_model=AdminMetricsResponse)
async def get_metrics(
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> AdminMetricsResponse:
    """GET /admin/metrics - simple platform counts: users by type, jobs
    active/hidden, applications total/hidden."""
    return AdminMetricsResponse.model_validate(await service.get_metrics())


@router.get("/system/status", response_model=SystemStatusResponse)
async def get_system_status(
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> SystemStatusResponse:
    """GET /admin/system/status - live backend health for the admin
    dashboard: persistence backend, provider config, a live DB
    connectivity check (not just "a pool object exists"), whether prod is
    still signing tokens with the published placeholder JWT secret, and how
    many evaluations this process currently has running.

    The database check never raises - a DB outage is reported as
    `database.connected=false` with `database.error` set, not a 500."""
    return SystemStatusResponse.model_validate(await service.get_system_status())


@router.post("/impersonate/{user_id}", response_model=ImpersonateResponse)
async def impersonate(
    user_id: str,
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> ImpersonateResponse:
    """POST /admin/impersonate/{user_id} - issue a normal access/refresh
    token pair for the target user, so the resulting session is
    indistinguishable from that user's own login. Writes exactly one
    AuditLogRecord (admin_id=the calling admin, action="impersonate",
    target_user_id=the target).

    Errors: 404 not_found if the target user does not exist; 400
    invalid_request if the target account is inactive.
    """
    access_token, refresh_token = await service.impersonate(principal.subject_id, user_id)
    return ImpersonateResponse(access_token=access_token, refresh_token=refresh_token)


__all__ = ["router"]
