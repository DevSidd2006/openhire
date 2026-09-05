"""
Authentication API routes.

Provides endpoints for user signup, login, token refresh, and self-service
profile management. Uses FastAPI dependency injection to wire
UserRepository and AppSettings.

Endpoints:
  - POST /auth/signup: Create new user account, return tokens + user
  - POST /auth/login: Authenticate user, return tokens + user
  - POST /auth/refresh: Exchange refresh token for new access token
  - GET /auth/me: The authenticated caller's own profile
  - PATCH /auth/me: Update the authenticated caller's own profile
  - POST /auth/me/password: Change the authenticated caller's own password
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status

from core.config import AppSettings, get_settings
from core.dependencies import get_user_repository
from core.logging import get_logger
from core.security import Principal, require_authenticated
from repositories.interfaces import UserRepository
from schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    RefreshTokenRequest,
    RefreshTokenResponse,
    SignupRequest,
    UpdateProfileRequest,
    UserProfileResponse,
    UserResponse,
)
from services.auth_service import AuthService

logger = get_logger("api.routes.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


def get_auth_service(
    user_repository: UserRepository = Depends(get_user_repository),
    settings: AppSettings = Depends(get_settings),
) -> AuthService:
    """Build AuthService with injected dependencies."""
    return AuthService(user_repository=user_repository, settings=settings)


@router.post("/signup", response_model=AuthResponse)
async def signup(
    request: SignupRequest,
    service: AuthService = Depends(get_auth_service),
) -> AuthResponse:
    """Create a new user account.

    Request body:
      - email: User's email address (must be unique)
      - password: Password (minimum 8 characters)
      - user_type: Either 'candidate' or 'recruiter'

    Returns:
      - user: User data (user_id, email, user_type)
      - access_token: JWT access token (valid for 15 minutes by default)
      - refresh_token: JWT refresh token (valid for 7 days by default)
      - token_type: Always "bearer"

    Raises:
      - 409 Conflict: Email already registered
      - 422 Unprocessable Entity: Invalid input (email format, password length, user_type)
    """
    user, access_token, refresh_token = await service.signup(
        email=request.email,
        password=request.password,
        user_type=request.user_type,
    )

    return AuthResponse(
        user=UserResponse(
            user_id=user.user_id,
            email=user.email,
            user_type=user.user_type,
        ),
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
    )


@router.post("/login", response_model=AuthResponse)
async def login(
    request: LoginRequest,
    service: AuthService = Depends(get_auth_service),
) -> AuthResponse:
    """Authenticate a user and return tokens.

    Request body:
      - email: User's email address
      - password: User's password

    Returns:
      - user: User data (user_id, email, user_type)
      - access_token: JWT access token (valid for 15 minutes by default)
      - refresh_token: JWT refresh token (valid for 7 days by default)
      - token_type: Always "bearer"

    Raises:
      - 401 Unauthorized: Email not found, password invalid, or account inactive
      - 422 Unprocessable Entity: Invalid input (email format)
    """
    user, access_token, refresh_token = await service.login(
        email=request.email,
        password=request.password,
    )

    return AuthResponse(
        user=UserResponse(
            user_id=user.user_id,
            email=user.email,
            user_type=user.user_type,
        ),
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
    )


@router.post("/refresh", response_model=RefreshTokenResponse)
async def refresh_token(
    request: RefreshTokenRequest,
    service: AuthService = Depends(get_auth_service),
) -> RefreshTokenResponse:
    """Refresh an access token.

    Request body:
      - refresh_token: Valid refresh token

    Returns:
      - access_token: New JWT access token (valid for 15 minutes by default)
      - token_type: Always "bearer"

    Raises:
      - 401 Unauthorized: Refresh token invalid, expired, or user not found/inactive
    """
    access_token = await service.refresh_access_token(request.refresh_token)

    return RefreshTokenResponse(
        access_token=access_token,
        token_type="bearer",
    )


@router.get("/me", response_model=UserProfileResponse)
async def get_my_profile(
    service: AuthService = Depends(get_auth_service),
    principal: Principal = Depends(require_authenticated),
) -> UserProfileResponse:
    """GET /auth/me - the authenticated caller's own full profile.

    Errors:
      - 401 Unauthorized: no valid credential presented
      - 404 not_found: the token's subject no longer has an account
        (should not happen in practice - the account that minted the token
        would have to have been deleted since)
    """
    user = await service.get_profile(principal.subject_id)
    return UserProfileResponse.from_record(user)


@router.patch("/me", response_model=UserProfileResponse)
async def update_my_profile(
    request: UpdateProfileRequest,
    service: AuthService = Depends(get_auth_service),
    principal: Principal = Depends(require_authenticated),
) -> UserProfileResponse:
    """PATCH /auth/me - partially update the authenticated caller's own profile.

    Only fields present in the request body are changed; an omitted field
    is left as-is, an explicit `null` clears it. email/user_type/is_active/
    password_hash cannot be set through this endpoint - they are not fields
    on `UpdateProfileRequest` at all.

    Errors:
      - 401 Unauthorized: no valid credential presented
      - 400 invalid_request: a candidate account supplied a recruiter-only
        field (company_name/company_website/company_role)
      - 404 not_found: the token's subject no longer has an account
    """
    updates = request.model_dump(exclude_unset=True)
    user = await service.update_profile(principal.subject_id, updates)
    return UserProfileResponse.from_record(user)


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_my_password(
    request: ChangePasswordRequest,
    service: AuthService = Depends(get_auth_service),
    principal: Principal = Depends(require_authenticated),
) -> None:
    """POST /auth/me/password - change the authenticated caller's own password.

    Errors:
      - 401 Unauthorized: no valid credential presented, OR current_password
        does not match
      - 404 not_found: the token's subject no longer has an account
      - 422 Unprocessable Entity: new_password shorter than 8 characters
    """
    await service.change_password(
        principal.subject_id, request.current_password, request.new_password
    )


__all__ = ["router"]
