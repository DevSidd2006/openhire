"""
Authentication request and response schemas.

Defines the request/response contracts for the authentication API endpoints:
signup, login, and token refresh.
"""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


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
    "AuthResponse",
    "RefreshTokenResponse",
]
