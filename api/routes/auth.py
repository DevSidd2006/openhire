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

import secrets
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from core.config import AppSettings, get_settings
from core.dependencies import get_user_repository
from core.errors import UnauthorizedError
from core.logging import get_logger
from core.security import Principal, require_authenticated
from repositories.interfaces import UserRecord, UserRepository
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
from services.linkedin_auth_service import LinkedInAuthService

logger = get_logger("api.routes.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


def get_auth_service(
    user_repository: UserRepository = Depends(get_user_repository),
    settings: AppSettings = Depends(get_settings),
) -> AuthService:
    """Build AuthService with injected dependencies."""
    return AuthService(user_repository=user_repository, settings=settings)


def get_linkedin_service(
    settings: AppSettings = Depends(get_settings),
) -> LinkedInAuthService:
    """Build LinkedInAuthService with injected dependencies."""
    return LinkedInAuthService(settings=settings)


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


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout_user(response: Response) -> dict[str, str]:
    """Expire the openhire_access_token and openhire_refresh_token authentication cookies."""
    response.delete_cookie(key="openhire_access_token", path="/", samesite="lax")
    response.delete_cookie(key="openhire_refresh_token", path="/", samesite="lax")
    response.delete_cookie(key="oauth_state", path="/", samesite="lax")
    return {"status": "logged_out"}


# ----------------------------------------------------------------------
# LinkedIn OAuth Endpoints
# ----------------------------------------------------------------------

# Short-lived in-memory storage for one-time OAuth exchange tickets (60s TTL)
_OAUTH_TICKETS: dict[str, dict[str, Any]] = {}


def _store_oauth_ticket(access_token: str, refresh_token: str, user: UserRecord) -> str:
    """Store tokens under a random one-time ticket expiring in 60 seconds."""
    now = time.time()
    # Prune expired tickets
    expired_keys = [k for k, v in _OAUTH_TICKETS.items() if v["expires_at"] < now]
    for k in expired_keys:
        _OAUTH_TICKETS.pop(k, None)

    ticket = secrets.token_urlsafe(32)
    _OAUTH_TICKETS[ticket] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": {
            "user_id": user.user_id,
            "email": user.email,
            "user_type": user.user_type,
        },
        "expires_at": now + 60,
    }
    return ticket


class ExchangeOAuthTicketRequest(BaseModel):
    ticket: str = Field(..., min_length=16, description="One-time OAuth exchange ticket")


@router.post("/linkedin/exchange", response_model=AuthResponse)
async def exchange_oauth_ticket(request: ExchangeOAuthTicketRequest) -> AuthResponse:
    """Exchange a short-lived one-time ticket for access and refresh tokens.

    Prevents placing JWT credentials directly into browser redirect URLs or logs.
    """
    now = time.time()
    ticket_data = _OAUTH_TICKETS.pop(request.ticket, None)
    if not ticket_data or ticket_data["expires_at"] < now:
        raise UnauthorizedError(
            "Invalid or expired OAuth ticket.",
            internal_detail="oauth_ticket_invalid_or_expired",
        )

    user_info = ticket_data["user"]
    return AuthResponse(
        user=UserResponse(
            user_id=user_info["user_id"],
            email=user_info["email"],
            user_type=user_info["user_type"],
        ),
        access_token=ticket_data["access_token"],
        refresh_token=ticket_data["refresh_token"],
        token_type="bearer",
    )


@router.get("/linkedin/authorize")
async def linkedin_authorize(
    role: str = Query("candidate", pattern="^(candidate|recruiter)$"),
    linkedin_service: LinkedInAuthService = Depends(get_linkedin_service),
) -> RedirectResponse:
    """Initiates LinkedIn OAuth login flow with CSRF state protection."""
    state_nonce = secrets.token_urlsafe(24)
    state = f"{state_nonce}:{role}"
    auth_url = linkedin_service.get_authorization_url(state=state)

    redirect_response = RedirectResponse(url=auth_url, status_code=status.HTTP_302_FOUND)
    # Store state nonce in short-lived SameSite cookie to prevent login CSRF
    redirect_response.set_cookie(
        key="oauth_state",
        value=state_nonce,
        httponly=True,
        samesite="lax",
        max_age=300,  # 5 minutes
    )
    return redirect_response


@router.get("/linkedin/callback")
async def linkedin_callback(
    request: Request,
    response: Response,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    linkedin_service: LinkedInAuthService = Depends(get_linkedin_service),
    user_repository: UserRepository = Depends(get_user_repository),
    auth_service: AuthService = Depends(get_auth_service),
) -> RedirectResponse:
    """Handles LinkedIn OAuth callback:
    - Verifies CSRF state cookie against returned state.
    - Fetches profile info (name, email, picture) from LinkedIn OIDC.
    - Rejects inactive accounts for existing users.
    - If user doesn't exist, creates an account with a random bcrypt password hash.
    - Issues one-time ticket and sets HttpOnly cookie (no tokens exposed in URL).
    """
    if error or not code:
        err_res = RedirectResponse(
            url="/app/login.html?error=linkedin_cancelled", status_code=status.HTTP_302_FOUND
        )
        err_res.delete_cookie("oauth_state")
        return err_res

    # 1. Verify CSRF state against initiating browser session
    cookie_nonce = request.cookies.get("oauth_state")
    if not state or not cookie_nonce or not state.startswith(f"{cookie_nonce}:"):
        logger.warning(
            "LinkedIn OAuth state mismatch or missing state cookie (CSRF validation failed)"
        )
        err_res = RedirectResponse(
            url="/app/login.html?error=csrf_validation_failed",
            status_code=status.HTTP_302_FOUND,
        )
        err_res.delete_cookie("oauth_state")
        return err_res

    # 2. Extract role from state
    user_type = "candidate"
    if ":" in state:
        _, role_param = state.split(":", 1)
        if role_param in ("candidate", "recruiter"):
            user_type = role_param

    # 3. Fetch LinkedIn UserInfo (with role-aware mock identity in mock mode)
    userinfo = await linkedin_service.exchange_code_for_userinfo(code, role=user_type)
    email = userinfo.get("email")
    if not email:
        err_res = RedirectResponse(
            url="/app/login.html?error=missing_email", status_code=status.HTTP_302_FOUND
        )
        err_res.delete_cookie("oauth_state")
        return err_res

    full_name = userinfo.get("name")
    avatar_url = userinfo.get("picture")

    # 4. Check if user already exists
    user = await user_repository.get_by_email(email)
    if user:
        # Check if existing account is disabled
        if not user.is_active:
            logger.warning(
                "LinkedIn login attempt with inactive account",
                extra={"user_id": user.user_id, "email": email},
            )
            raise UnauthorizedError(
                "This account is not active.",
                internal_detail=f"account {user.user_id!r} is inactive",
            )

        # Update full_name and avatar if not already set
        updates = {}
        if not user.full_name and full_name:
            updates["full_name"] = full_name
        if not user.avatar_url and avatar_url:
            updates["avatar_url"] = avatar_url
        if updates:
            user = await auth_service.update_profile(user.user_id, updates)
    else:
        # Generate random password and hash with bcrypt to satisfy NOT NULL DB constraint
        random_password = secrets.token_urlsafe(32)
        password_hash = auth_service._hash_password(random_password)

        user_id = f"user_{uuid.uuid4().hex[:8]}"
        user = UserRecord(
            user_id=user_id,
            email=email,
            password_hash=password_hash,
            user_type=user_type,
            full_name=full_name,
            avatar_url=avatar_url,
            is_active=True,
        )
        user = await user_repository.save(user)

    # 5. Mint OpenHire access & refresh tokens
    access_token, refresh_token = auth_service.issue_tokens_for_user(user)

    # 6. Generate one-time exchange ticket to avoid credentials in the URL
    ticket = _store_oauth_ticket(access_token, refresh_token, user)

    # 7. Redirect target with one-time ticket (safe against browser history and log leaks)
    target_dashboard = "recruiter.html" if user.user_type == "recruiter" else "candidate.html"
    redirect_target = f"/app/{target_dashboard}?ticket={ticket}"

    redirect_response = RedirectResponse(url=redirect_target, status_code=status.HTTP_302_FOUND)

    # 8. Set Dual-Auth cookie (openhire_access_token)
    redirect_response.set_cookie(
        key="openhire_access_token",
        value=access_token,
        httponly=True,
        samesite="lax",
        max_age=15 * 60,
    )
    redirect_response.delete_cookie("oauth_state")
    return redirect_response


__all__ = ["router"]
