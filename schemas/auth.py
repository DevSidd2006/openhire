"""
Authentication request and response schemas.

Defines the request/response contracts for the authentication API endpoints:
signup, login, and token refresh.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field

from repositories.interfaces import UserRecord


class SignupRequest(BaseModel):
    """Request to create a new user account."""

    email: EmailStr
    password: str = Field(min_length=8, description="Password must be at least 8 characters")
    user_type: str = Field(pattern="^(candidate|recruiter)$", description="Either 'candidate' or 'recruiter'")


class LoginRequest(BaseModel):
    """Request to authenticate an existing user."""

    email: EmailStr
    password: str


class RefreshTokenRequest(BaseModel):
    """Request to refresh an access token."""

    refresh_token: str


class TokenResponse(BaseModel):
    """Response containing authentication tokens."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    """User data in responses."""

    user_id: str
    email: str
    user_type: str


class UserProfileResponse(BaseModel):
    """Full self-service profile view, returned by GET/PATCH /auth/me.

    Deliberately excludes password_hash - UserResponse (above) stays the
    minimal shape used in auth flows; this is the richer shape for the
    profile page. Never includes candidate resume data (skills, experience,
    education): that comes from GET /candidates/me and is read-only here.
    """

    user_id: str
    email: str
    user_type: str
    created_at: datetime

    full_name: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    headline: Optional[str] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None

    company_name: Optional[str] = None
    company_website: Optional[str] = None
    company_role: Optional[str] = None

    @classmethod
    def from_record(cls, record: UserRecord) -> "UserProfileResponse":
        return cls(
            user_id=record.user_id,
            email=record.email,
            user_type=record.user_type,
            created_at=record.created_at,
            full_name=record.full_name,
            phone=record.phone,
            location=record.location,
            headline=record.headline,
            bio=record.bio,
            avatar_url=record.avatar_url,
            company_name=record.company_name,
            company_website=record.company_website,
            company_role=record.company_role,
        )


class UpdateProfileRequest(BaseModel):
    """PATCH /auth/me request body.

    Every field is optional so a caller can send only what changed.
    `model_dump(exclude_unset=True)` (used by the route) distinguishes an
    omitted field (left alone) from an explicit `null` (clears it) - a
    plain default-based dump could not tell these apart.

    No email/user_type/is_active/password_hash field exists here at all,
    so a client cannot even attempt to set them through this endpoint.
    """

    full_name: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    headline: Optional[str] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None

    company_name: Optional[str] = None
    company_website: Optional[str] = None
    company_role: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    """POST /auth/me/password request body."""

    current_password: str
    new_password: str = Field(min_length=8, description="Password must be at least 8 characters")


class AuthResponse(BaseModel):
    """Complete response from signup/login endpoints."""

    user: UserResponse
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshTokenResponse(BaseModel):
    """Response from token refresh endpoint."""

    access_token: str
    token_type: str = "bearer"


__all__ = [
    "SignupRequest",
    "LoginRequest",
    "RefreshTokenRequest",
    "TokenResponse",
    "UserResponse",
    "UserProfileResponse",
    "UpdateProfileRequest",
    "ChangePasswordRequest",
    "AuthResponse",
    "RefreshTokenResponse",
]
