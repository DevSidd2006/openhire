"""
LinkedIn OpenID Connect (OIDC) Service.

Handles authorization URL generation, code-to-token exchange, and userinfo
retrieval from https://api.linkedin.com/v1/userinfo.
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
        self.client_id = settings.linkedin_client_id
        self.client_secret = settings.linkedin_client_secret
        self.redirect_uri = settings.linkedin_redirect_uri

    @property
    def is_mock_mode(self) -> bool:
        """Returns True if LinkedIn credentials are not configured."""
        return not bool(self.client_id and self.client_secret)

    def get_authorization_url(self, state: str) -> str:
        """Build LinkedIn OIDC authorization URL or redirect to mock callback."""
        if self.is_mock_mode:
            logger.info("LinkedIn credentials unset: operating in offline MOCK mode")
            return f"{self.redirect_uri}?code=mock_linkedin_code_123&state={urllib.parse.quote(state)}"

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": "openid profile email",
            "state": state,
        }
        return f"{LINKEDIN_AUTH_URL}?{urllib.parse.urlencode(params)}"

    async def exchange_code_for_userinfo(self, code: str) -> Dict[str, Any]:
        """Exchange authorization code for userinfo via OIDC."""
        if self.is_mock_mode or code.startswith("mock_"):
            logger.info("Using mock LinkedIn userinfo profile")
            return {
                "sub": "mock_linkedin_sub_001",
                "name": "Jane Candidate",
                "given_name": "Jane",
                "family_name": "Candidate",
                "email": "jane.candidate@example.com",
                "email_verified": True,
                "picture": "https://api.dicebear.com/7.x/avataaars/svg?seed=Jane",
            }

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

            token_data = token_response.json()
            access_token = token_data.get("access_token")

            # 2. Fetch UserInfo from https://api.linkedin.com/v1/userinfo
            userinfo_response = await client.get(
                LINKEDIN_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if userinfo_response.status_code != 200:
                logger.error(f"LinkedIn userinfo request failed: {userinfo_response.text}")
                raise BadRequestError("Failed to fetch LinkedIn profile information.")

            return userinfo_response.json()
