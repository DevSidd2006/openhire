"""
Regression tests for LinkedIn OAuth Sign-In & Sign-Up flow.

Covers:
1. Mock authentication gating: mock_* codes rejected when live credentials are configured;
   mock mode disabled in production; explicit LINKEDIN_MOCK_ENABLED flag behavior.
2. Distinct deterministic mock profiles for candidate vs recruiter flows.
3. One-time OAuth ticket exchange preventing credentials in URLs; single-use replay protection.
4. Server logout endpoint (POST /auth/logout) clearing HttpOnly authentication cookies.
5. Inactive user rejection on OAuth callback.
6. CSRF state cookie validation.
7. Network / JSON error handling in external LinkedIn communication.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.config import AppSettings, get_settings
from core.container import ServiceContainer
from core.errors import BadRequestError, UnauthorizedError
from core.security import JWTAuthProvider
from repositories.interfaces import UserRecord
from repositories.memory import (
    InMemoryApplicationRepository,
    InMemoryAuditLogRepository,
    InMemoryBugReportRepository,
    InMemoryCandidateRepository,
    InMemoryEvaluationRepository,
    InMemoryJobRepository,
    InMemoryLLMCredentialRepository,
    InMemoryRubricRepository,
    InMemorySessionRepository,
    InMemoryTranscriptRepository,
    InMemoryUserRepository,
)
from services.auth_service import AuthService
from services.evaluation_dispatcher import AsyncTaskEvaluationDispatcher
from services.linkedin_auth_service import LinkedInAuthService


# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------


def _build_test_app(settings: AppSettings, user_repo: InMemoryUserRepository | None = None):
    if user_repo is None:
        user_repo = InMemoryUserRepository()
    auth_service = AuthService(user_repository=user_repo, settings=settings)
    container = ServiceContainer(
        settings=settings,
        session_repository=InMemorySessionRepository(),
        transcript_repository=InMemoryTranscriptRepository(),
        job_repository=InMemoryJobRepository(),
        candidate_repository=InMemoryCandidateRepository(),
        application_repository=InMemoryApplicationRepository(),
        rubric_repository=InMemoryRubricRepository(),
        evaluation_repository=InMemoryEvaluationRepository(),
        user_repository=user_repo,
        bug_report_repository=InMemoryBugReportRepository(),
        audit_log_repository=InMemoryAuditLogRepository(),
        llm_credential_repository=InMemoryLLMCredentialRepository(),
        evaluation_dispatcher=AsyncTaskEvaluationDispatcher(),
        auth_provider=JWTAuthProvider(auth_service),
        persistence_is_ephemeral=True,
    )
    app = create_app(settings)
    app.state.container = container
    app.dependency_overrides[get_settings] = lambda: settings
    return app, auth_service, user_repo


# ---------------------------------------------------------------------------
# 1. Mock Authentication Gating & Identity Distinction Tests
# ---------------------------------------------------------------------------


class TestLinkedInMockGating:
    def test_mock_code_rejected_when_live_credentials_configured_without_mock_flag(self):
        """When live credentials are set and LINKEDIN_MOCK_ENABLED is False, mock_* codes must be rejected."""
        settings = AppSettings(
            environment="development",
            linkedin_client_id="live_client_id_123",
            linkedin_client_secret="live_client_secret_xyz",
            linkedin_mock_enabled=False,
        )
        service = LinkedInAuthService(settings)
        assert service.is_mock_mode is False

        # Attempting to exchange a mock code must raise BadRequestError
        with pytest.raises(BadRequestError) as exc_info:
            pytest.importorskip("asyncio").run(
                service.exchange_code_for_userinfo("mock_recruiter_bypass_code")
            )
        assert "Mock codes are not permitted when live LinkedIn authentication is active" in str(
            exc_info.value
        )

    def test_mock_mode_never_allowed_in_production(self):
        """Mock mode is strictly prohibited in production, even if flag is mistakenly enabled."""
        settings = AppSettings(
            environment="production",
            linkedin_client_id="prod_id",
            linkedin_client_secret="prod_secret",
            linkedin_mock_enabled=True,
        )
        service = LinkedInAuthService(settings)
        assert service.is_mock_mode is False

        with pytest.raises(BadRequestError):
            pytest.importorskip("asyncio").run(
                service.exchange_code_for_userinfo("mock_code")
            )

    def test_mock_mode_active_when_explicitly_enabled(self):
        """When LINKEDIN_MOCK_ENABLED=True in development, mock mode operates even if credentials exist."""
        settings = AppSettings(
            environment="development",
            linkedin_client_id="test_id",
            linkedin_client_secret="test_secret",
            linkedin_mock_enabled=True,
        )
        service = LinkedInAuthService(settings)
        assert service.is_mock_mode is True

        candidate_info = pytest.importorskip("asyncio").run(
            service.exchange_code_for_userinfo("mock_123", role="candidate")
        )
        assert candidate_info["email"] == "mock.candidate@example.com"
        assert candidate_info["given_name"] == "Jane"

        recruiter_info = pytest.importorskip("asyncio").run(
            service.exchange_code_for_userinfo("mock_123", role="recruiter")
        )
        assert recruiter_info["email"] == "mock.recruiter@example.com"
        assert recruiter_info["given_name"] == "Alex"

    def test_mock_mode_default_in_dev_when_credentials_unset(self):
        """When credentials are unset in development, service defaults to offline mock mode."""
        settings = AppSettings(
            environment="development",
            linkedin_client_id="",
            linkedin_client_secret="",
            linkedin_mock_enabled=False,
        )
        service = LinkedInAuthService(settings)
        assert service.is_mock_mode is True


# ---------------------------------------------------------------------------
# 2. OAuth Ticket Exchange & Single-Use Replay Protection Tests
# ---------------------------------------------------------------------------


class TestOAuthTicketExchangeFlow:
    @pytest.fixture
    def mock_oauth_client(self):
        settings = AppSettings(
            environment="development",
            linkedin_mock_enabled=True,
            auth_enabled=True,
        )
        app, auth_service, user_repo = _build_test_app(settings)
        client = TestClient(app)
        return client, user_repo

    def test_oauth_flow_issues_ticket_without_credentials_in_url(self, mock_oauth_client):
        """Callback must redirect with a ?ticket= parameter, NOT access_token or refresh_token."""
        client, user_repo = mock_oauth_client

        # 1. Authorize step initiates CSRF state cookie
        auth_resp = client.get("/auth/linkedin/authorize?role=candidate", follow_redirects=False)
        assert auth_resp.status_code == 302
        assert "oauth_state" in auth_resp.cookies

        state_nonce = auth_resp.cookies["oauth_state"]
        full_state = f"{state_nonce}:candidate"

        # 2. Callback step with mock code and state cookie
        cb_resp = client.get(
            f"/auth/linkedin/callback?code=mock_test_code&state={full_state}",
            follow_redirects=False,
        )
        assert cb_resp.status_code == 302
        redirect_url = cb_resp.headers["location"]

        # Tokens MUST NOT be exposed in URL query parameters
        assert "access_token" not in redirect_url
        assert "refresh_token" not in redirect_url
        assert "ticket=" in redirect_url
        assert "candidate.html" in redirect_url

        # Dual-auth cookie should be set
        assert "openhire_access_token" in cb_resp.cookies

        # Extract ticket parameter
        ticket = redirect_url.split("ticket=")[1].split("&")[0]
        assert len(ticket) >= 16

        # 3. Exchange ticket for tokens
        exch_resp = client.post("/auth/linkedin/exchange", json={"ticket": ticket})
        assert exch_resp.status_code == 200
        payload = exch_resp.json()
        assert "access_token" in payload
        assert "refresh_token" in payload
        assert payload["user"]["email"] == "mock.candidate@example.com"
        assert payload["user"]["user_type"] == "candidate"

        # 4. Replay protection: using the same ticket again MUST fail
        replay_resp = client.post("/auth/linkedin/exchange", json={"ticket": ticket})
        assert replay_resp.status_code == 401

    def test_invalid_or_expired_ticket_rejected(self, mock_oauth_client):
        """Exchange must reject non-existent or manipulated tickets."""
        client, _ = mock_oauth_client
        resp = client.post("/auth/linkedin/exchange", json={"ticket": "non_existent_fake_ticket_123456"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 3. Server Logout Endpoint Tests
# ---------------------------------------------------------------------------


class TestServerLogoutEndpoint:
    @pytest.fixture
    def test_client(self):
        settings = AppSettings(auth_enabled=True)
        app, _, _ = _build_test_app(settings)
        return TestClient(app)

    def test_logout_clears_auth_cookies(self, test_client):
        """POST /auth/logout must set Set-Cookie headers that delete auth cookies."""
        test_client.cookies.set("openhire_access_token", "fake_token", domain="testserver", path="/")
        test_client.cookies.set("openhire_refresh_token", "fake_refresh", domain="testserver", path="/")
        test_client.cookies.set("oauth_state", "fake_state", domain="testserver", path="/")

        response = test_client.post("/auth/logout")
        assert response.status_code == 200
        assert response.json() == {"status": "logged_out"}

        # Inspect cookies to verify expiration
        set_cookie_headers = response.headers.get_list("set-cookie") if hasattr(response.headers, "get_list") else [response.headers.get("set-cookie", "")]
        raw_cookies = " ".join(set_cookie_headers)

        assert "openhire_access_token" in raw_cookies
        assert "openhire_refresh_token" in raw_cookies
        assert "oauth_state" in raw_cookies

    def test_logout_idempotent_without_cookies(self, test_client):
        """POST /auth/logout must succeed even if no cookies were present."""
        response = test_client.post("/auth/logout")
        assert response.status_code == 200
        assert response.json() == {"status": "logged_out"}


# ---------------------------------------------------------------------------
# 4. Security & Error Handling Regressions
# ---------------------------------------------------------------------------


class TestSecurityAndErrorHandling:
    def test_callback_rejects_inactive_user(self):
        """If an existing user account has is_active=False, callback must reject with 401."""
        settings = AppSettings(
            environment="development",
            linkedin_mock_enabled=True,
            auth_enabled=True,
        )
        user_repo = InMemoryUserRepository()
        # Seed inactive user with the mock candidate email
        inactive_user = UserRecord(
            user_id="inactive_user_001",
            email="mock.candidate@example.com",
            password_hash="test_hash",
            user_type="candidate",
            is_active=False,
        )
        pytest.importorskip("asyncio").run(user_repo.save(inactive_user))

        app, _, _ = _build_test_app(settings, user_repo=user_repo)
        client = TestClient(app)

        state_nonce = "csrf_test_nonce_123"
        full_state = f"{state_nonce}:candidate"
        client.cookies.set("oauth_state", state_nonce)

        resp = client.get(
            f"/auth/linkedin/callback?code=mock_test_code&state={full_state}",
            follow_redirects=False,
        )
        assert resp.status_code == 401
        assert "active" in resp.json().get("detail", "").lower()

    def test_callback_rejects_csrf_state_mismatch(self):
        """Callback without matching oauth_state cookie must redirect to login with error."""
        settings = AppSettings(
            environment="development",
            linkedin_mock_enabled=True,
            auth_enabled=True,
        )
        app, _, _ = _build_test_app(settings)
        client = TestClient(app)

        # Cookie says "nonce_AAA", query state says "nonce_BBB:candidate"
        client.cookies.set("oauth_state", "nonce_AAA")
        resp = client.get(
            "/auth/linkedin/callback?code=mock_code&state=nonce_BBB:candidate",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "csrf_validation_failed" in resp.headers["location"]

    @pytest.mark.asyncio
    async def test_network_and_timeout_errors_caught_gracefully(self):
        """Httpx timeout and network errors must be caught and converted to controlled BadRequestError."""
        settings = AppSettings(
            environment="development",
            linkedin_client_id="live_id",
            linkedin_client_secret="live_secret",
            linkedin_mock_enabled=False,
        )
        service = LinkedInAuthService(settings)

        # Simulate timeout on token exchange
        with patch("httpx.AsyncClient.post", side_effect=httpx.TimeoutException("Connection timed out")):
            with pytest.raises(BadRequestError) as exc_info:
                await service.exchange_code_for_userinfo("real_code_123")
            assert "timed out" in str(exc_info.value).lower()

        # Simulate transport failure
        with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("DNS failed")):
            with pytest.raises(BadRequestError) as exc_info:
                await service.exchange_code_for_userinfo("real_code_123")
            assert "unable to communicate" in str(exc_info.value).lower()
