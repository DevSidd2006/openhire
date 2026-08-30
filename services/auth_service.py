"""
Authentication service implementation.

Handles user signup, login, token generation and validation. Integrates with
UserRepository for persistence and AppSettings for configuration.

Methods provided:
  - signup(email, password, user_type): Create a new user account and generate tokens.
  - login(email, password): Authenticate user and return access/refresh tokens.
  - refresh_access_token(refresh_token): Generate a new access token from refresh token.
  - verify_access_token(token): Decode and validate JWT access token, return Principal.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import bcrypt
import jwt

from core.config import AppSettings
from core.errors import ConflictError, UnauthorizedError
from core.logging import get_logger, log_context
from core.security import Principal, PrincipalType
from repositories.interfaces import UserRecord, UserRepository

logger = get_logger("services.auth")


class AuthService:
    """Authentication and token management service."""

    def __init__(self, *, user_repository: UserRepository, settings: AppSettings) -> None:
        self._users = user_repository
        self._settings = settings

    async def signup(
        self, email: str, password: str, user_type: str
    ) -> Tuple[UserRecord, str, str]:
        """Create a new user account and generate tokens.

        Args:
            email: User email address.
            password: User password (will be hashed with bcrypt).
            user_type: Account type ('candidate' or 'recruiter').

        Returns:
            Tuple of (UserRecord, access_token, refresh_token)

        Raises:
            ConflictError: If email is already registered.
            UnauthorizedError: If input validation fails.
        """
        # Check if email is already registered
        existing = await self._users.get_by_email(email)
        if existing is not None:
            logger.warning(
                "signup attempt with existing email",
                extra=log_context(event="signup_email_conflict", email=email),
            )
            raise ConflictError(
                "An account with this email already exists.",
                internal_detail=f"email {email!r} already registered",
            )

        # Generate user_id and hash password
        user_id = f"user_{uuid.uuid4().hex[:8]}"
        password_hash = self._hash_password(password)

        # Create and store user record
        user_record = UserRecord(
            user_id=user_id,
            email=email,
            password_hash=password_hash,
            user_type=user_type,
            is_active=True,
        )
        stored_user = await self._users.save(user_record)

        # Generate tokens
        access_token = self._generate_access_token(user_id, user_type)
        refresh_token = self._generate_refresh_token(user_id)

        logger.info(
            "user signed up",
            extra=log_context(
                event="user_signup",
                user_id=user_id,
                user_type=user_type,
                email=email,
            ),
        )

        return stored_user, access_token, refresh_token

    async def login(self, email: str, password: str) -> Tuple[UserRecord, str, str]:
        """Authenticate user and return tokens.

        Args:
            email: User email address.
            password: User password.

        Returns:
            Tuple of (UserRecord, access_token, refresh_token)

        Raises:
            UnauthorizedError: If email not found or password is invalid.
        """
        user = await self._users.get_by_email(email)
        if user is None:
            logger.warning(
                "login attempt with unknown email",
                extra=log_context(event="login_user_not_found", email=email),
            )
            raise UnauthorizedError(
                "Invalid email or password.",
                internal_detail=f"email {email!r} not found",
            )

        # Verify password
        if not self._verify_password(password, user.password_hash):
            logger.warning(
                "login attempt with wrong password",
                extra=log_context(event="login_invalid_password", user_id=user.user_id),
            )
            raise UnauthorizedError(
                "Invalid email or password.",
                internal_detail=f"password verification failed for user {user.user_id!r}",
            )

        # Check if account is active
        if not user.is_active:
            logger.warning(
                "login attempt with inactive account",
                extra=log_context(event="login_inactive_account", user_id=user.user_id),
            )
            raise UnauthorizedError(
                "This account is not active.",
                internal_detail=f"account {user.user_id!r} is inactive",
            )

        # Generate tokens
        access_token = self._generate_access_token(user.user_id, user.user_type)
        refresh_token = self._generate_refresh_token(user.user_id)

        logger.info(
            "user logged in",
            extra=log_context(
                event="user_login",
                user_id=user.user_id,
                email=email,
            ),
        )

        return user, access_token, refresh_token

    async def refresh_access_token(self, refresh_token: str) -> str:
        """Generate a new access token from a refresh token.

        Args:
            refresh_token: Valid refresh token.

        Returns:
            New access token.

        Raises:
            UnauthorizedError: If refresh token is invalid or expired.
        """
        try:
            payload = jwt.decode(
                refresh_token,
                self._settings.jwt_secret_key,
                algorithms=["HS256"],
            )
        except jwt.InvalidTokenError as exc:
            logger.warning(
                "refresh token validation failed",
                extra=log_context(event="refresh_token_invalid"),
            )
            raise UnauthorizedError(
                "Invalid or expired refresh token.",
                internal_detail=f"refresh token decode failed: {exc}",
            ) from exc

        # Extract subject (user_id) and user_type from token
        user_id = payload.get("sub")
        user_type = payload.get("user_type")
        token_type = payload.get("type")

        if not user_id or token_type != "refresh":
            logger.warning(
                "refresh token has invalid payload",
                extra=log_context(event="refresh_token_invalid_payload"),
            )
            raise UnauthorizedError(
                "Invalid refresh token.",
                internal_detail="refresh token missing subject or has wrong type",
            )

        # Verify user still exists and is active
        user = await self._users.get_by_id(user_id)
        if user is None or not user.is_active:
            logger.warning(
                "refresh token for non-existent or inactive user",
                extra=log_context(event="refresh_token_user_not_found", user_id=user_id),
            )
            raise UnauthorizedError(
                "User account not found or is inactive.",
                internal_detail=f"user {user_id!r} does not exist or is inactive",
            )

        # Generate new access token
        access_token = self._generate_access_token(user_id, user_type or user.user_type)

        logger.info(
            "access token refreshed",
            extra=log_context(event="access_token_refreshed", user_id=user_id),
        )

        return access_token

    async def verify_access_token(self, token: str) -> Principal:
        """Decode and validate JWT access token, return Principal.

        Args:
            token: JWT access token to verify.

        Returns:
            Principal object representing the authenticated user.

        Raises:
            UnauthorizedError: If token is invalid or expired.
        """
        try:
            payload = jwt.decode(
                token,
                self._settings.jwt_secret_key,
                algorithms=["HS256"],
            )
        except jwt.InvalidTokenError as exc:
            logger.warning(
                "access token validation failed",
                extra=log_context(event="access_token_invalid"),
            )
            raise UnauthorizedError(
                "Invalid or expired access token.",
                internal_detail=f"access token decode failed: {exc}",
            ) from exc

        # Extract claims from token
        user_id = payload.get("sub")
        user_type = payload.get("user_type")
        token_type = payload.get("type")

        if not user_id or token_type != "access":
            logger.warning(
                "access token has invalid payload",
                extra=log_context(event="access_token_invalid_payload"),
            )
            raise UnauthorizedError(
                "Invalid access token.",
                internal_detail="access token missing subject or has wrong type",
            )

        # Map user_type to PrincipalType
        principal_type = self._map_user_type_to_principal_type(user_type)

        return Principal(
            principal_type=principal_type,
            subject_id=user_id,
            scopes=frozenset(),  # Scopes can be populated based on user_type or other logic
        )

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _hash_password(self, password: str) -> str:
        """Hash password using bcrypt with cost factor 12.

        Args:
            password: Plain text password.

        Returns:
            Hashed password string.
        """
        salt = bcrypt.gensalt(rounds=12)
        hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
        return hashed.decode("utf-8")

    def _verify_password(self, password: str, password_hash: str) -> bool:
        """Verify plain text password against hash.

        Args:
            password: Plain text password to verify.
            password_hash: Hashed password to compare against.

        Returns:
            True if password matches hash, False otherwise.
        """
        try:
            return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
        except (ValueError, TypeError):
            # Invalid hash format
            return False

    def _generate_access_token(self, user_id: str, user_type: str) -> str:
        """Generate a JWT access token.

        Args:
            user_id: User identifier.
            user_type: User type (candidate or recruiter).

        Returns:
            Encoded JWT access token.
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=self._settings.access_token_expire_minutes)

        payload = {
            "sub": user_id,
            "user_type": user_type,
            "type": "access",
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }

        token = jwt.encode(
            payload,
            self._settings.jwt_secret_key,
            algorithm="HS256",
        )
        return token

    def _generate_refresh_token(self, user_id: str) -> str:
        """Generate a JWT refresh token.

        Args:
            user_id: User identifier.

        Returns:
            Encoded JWT refresh token.
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=self._settings.refresh_token_expire_days)

        payload = {
            "sub": user_id,
            "type": "refresh",
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }

        token = jwt.encode(
            payload,
            self._settings.jwt_secret_key,
            algorithm="HS256",
        )
        return token

    @staticmethod
    def _map_user_type_to_principal_type(user_type: str) -> PrincipalType:
        """Map user_type string to PrincipalType enum.

        Args:
            user_type: User type string ('candidate' or 'recruiter').

        Returns:
            Corresponding PrincipalType enum value.
        """
        if user_type == "candidate":
            return PrincipalType.CANDIDATE
        elif user_type == "recruiter":
            return PrincipalType.RECRUITER
        else:
            # Default to ANONYMOUS for unknown types
            return PrincipalType.ANONYMOUS


__all__ = ["AuthService"]
