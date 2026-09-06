"""
Admin console request/response schemas.

Reuses `UserProfileResponse` (schemas/auth.py) rather than inventing a
second "user view" - the admin console needs the exact same fields (no
password_hash, every profile field), just for an arbitrary user instead of
only the caller's own. `AdminUpdateUserRequest` is intentionally NOT
`UpdateProfileRequest`: it exposes `is_active`/`user_type`, which a
self-service profile update must never be able to touch (see
services/auth_service.py:_PROTECTED_ACCOUNT_FIELDS) - the two request
shapes are deliberately disjoint, not one widened to cover both roles.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from repositories.interfaces import UserRecord
from schemas.auth import UserProfileResponse


# ---------------------------------------------------------------------------
# GET /admin/users
# ---------------------------------------------------------------------------

class AdminUserListResponse(BaseModel):
    users: List[UserProfileResponse]
    total: int

    @classmethod
    def from_records(cls, records: List[UserRecord]) -> "AdminUserListResponse":
        return cls(
            users=[UserProfileResponse.from_record(r) for r in records],
            total=len(records),
        )


# ---------------------------------------------------------------------------
# PATCH /admin/users/{user_id}
# ---------------------------------------------------------------------------

class AdminUpdateUserRequest(BaseModel):
    """Every field optional so an admin can change just one. Validated
    further in AdminService.update_user (user_type must be one of
    candidate/recruiter/admin) - schema-level validation can't express
    "one of these three strings AND report 400 with a client-safe message",
    since FastAPI's own 422 for a failed `pattern=` is that "invalid enum
    value" shape, not this endpoint's `BadRequestError` contract."""

    is_active: Optional[bool] = None
    user_type: Optional[str] = None


# ---------------------------------------------------------------------------
# PATCH /admin/jobs/{job_id}
# ---------------------------------------------------------------------------

class AdminSetJobActiveRequest(BaseModel):
    is_active: bool


# ---------------------------------------------------------------------------
# PATCH /admin/candidates/{candidate_id}, PATCH /admin/applications/{application_id}
# ---------------------------------------------------------------------------

class AdminSetHiddenRequest(BaseModel):
    is_hidden: bool


# ---------------------------------------------------------------------------
# GET /admin/metrics
# ---------------------------------------------------------------------------

class UserTypeCounts(BaseModel):
    candidate: int = 0
    recruiter: int = 0
    admin: int = 0


class JobCounts(BaseModel):
    active: int = 0
    hidden: int = 0


class ApplicationCounts(BaseModel):
    total: int = 0
    hidden: int = 0


class AdminMetricsResponse(BaseModel):
    users: UserTypeCounts
    jobs: JobCounts
    applications: ApplicationCounts


# ---------------------------------------------------------------------------
# POST /admin/impersonate/{user_id}
# ---------------------------------------------------------------------------

class ImpersonateResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


__all__ = [
    "AdminUserListResponse",
    "AdminUpdateUserRequest",
    "AdminSetJobActiveRequest",
    "AdminSetHiddenRequest",
    "UserTypeCounts",
    "JobCounts",
    "ApplicationCounts",
    "AdminMetricsResponse",
    "ImpersonateResponse",
]
