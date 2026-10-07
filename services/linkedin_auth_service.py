"""
LinkedIn OpenID Connect (OIDC) Service.

Handles authorization URL generation, code-to-token exchange, and userinfo
retrieval from https://api.linkedin.com/v2/userinfo.
Includes offline Mock Mode when LINKEDIN_CLIENT_ID / SECRET are not configured.
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Dict
import httpx

from core.config import AppSettings
from core.errors import BadRequestError
from core.logging import get_logger

logger = get_logger("services.linkedin_auth")

LINKEDIN_AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"


class LinkedInAuthService:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.client_id = settings.linkedin_client_id
        self.client_secret = settings.linkedin_client_secret
        self.redirect_uri = settings.linkedin_redirect_uri

    @property
    def is_mock_mode(self) -> bool:
        """Gated mock authentication mode.

        Mock authentication is ONLY allowed when:
        1. Environment is not production.
        2. AND either:
           a) Mock mode is explicitly enabled via LINKEDIN_MOCK_ENABLED=true development setting.
           b) Credentials (client_id / client_secret) are unset in development.
        When live credentials are configured, live authentication MUST be used unless
        LINKEDIN_MOCK_ENABLED is explicitly set to True in development.
        """
        if self.settings.environment == "production":
            return False
        if self.client_id and self.client_secret:
            return self.settings.linkedin_mock_enabled
        return self.settings.environment == "development" or self.settings.linkedin_mock_enabled

    def get_authorization_url(self, state: str) -> str:
        """Build LinkedIn OIDC authorization URL or redirect to mock callback."""
        if self.is_mock_mode:
            logger.info("Operating in offline MOCK LinkedIn mode")
            return f"{self.redirect_uri}?code=mock_linkedin_code_123&state={urllib.parse.quote(state)}"

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": "openid profile email",
            "state": state,
        }
        return f"{LINKEDIN_AUTH_URL}?{urllib.parse.urlencode(params)}"

    async def exchange_code_for_userinfo(
        self, code: str, role: str = "candidate"
    ) -> Dict[str, Any]:
        """Exchange authorization code for userinfo via OIDC.

        In mock mode, provides distinct deterministic identities for candidate
        and recruiter signups so each flow can be tested independently.
        """
        if not self.is_mock_mode:
            if code.startswith("mock_"):
                logger.warning("Rejected mock OAuth code when live LinkedIn credentials are active")
                raise BadRequestError(
                    "Mock codes are not permitted when live LinkedIn authentication is active."
                )

        if self.is_mock_mode:
            logger.info("Using mock LinkedIn userinfo profile", extra={"role": role})
            if role == "recruiter":
                return {
                    "sub": "mock_linkedin_recruiter_001",
                    "name": "Alex Recruiter",
                    "given_name": "Alex",
                    "family_name": "Recruiter",
                    "email": "mock.recruiter@example.com",
                    "email_verified": True,
                    "picture": "https://api.dicebear.com/7.x/avataaars/svg?seed=Alex",
                }
            return {
                "sub": "mock_linkedin_candidate_001",
                "name": "Jane Candidate",
                "given_name": "Jane",
                "family_name": "Candidate",
                "email": "mock.candidate@example.com",
                "email_verified": True,
                "picture": "https://api.dicebear.com/7.x/avataaars/svg?seed=Jane",
            }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # 1. Exchange code for access token
                token_response = await client.post(
                    LINKEDIN_TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": self.redirect_uri,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if token_response.status_code != 200:
                    logger.error(f"LinkedIn token exchange failed: {token_response.text}")
                    raise BadRequestError("Failed to obtain LinkedIn access token.")

                try:
                    token_data = token_response.json()
                except Exception:
                    logger.error("Failed to parse LinkedIn token response JSON")
                    raise BadRequestError("Invalid token response received from LinkedIn.")

                access_token = token_data.get("access_token")
                if not access_token:
                    logger.error("LinkedIn token response missing access_token field")
                    raise BadRequestError("LinkedIn access token missing from response.")

                # 2. Fetch UserInfo from https://api.linkedin.com/v2/userinfo
                userinfo_response = await client.get(
                    LINKEDIN_USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if userinfo_response.status_code != 200:
                    logger.error(f"LinkedIn userinfo request failed: {userinfo_response.text}")
                    raise BadRequestError("Failed to fetch LinkedIn profile information.")

                try:
                    return userinfo_response.json()
                except Exception:
                    logger.error("Failed to parse LinkedIn userinfo response JSON")
                    raise BadRequestError("Invalid profile data received from LinkedIn.")

        except httpx.TimeoutException:
            logger.error("LinkedIn API request timed out")
            raise BadRequestError("LinkedIn authentication request timed out. Please try again.")
        except httpx.RequestError as exc:
            logger.error(f"LinkedIn API network communication error: {type(exc).__name__}")
            raise BadRequestError("Unable to communicate with LinkedIn servers. Please try again.")
