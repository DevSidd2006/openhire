"""
Auth logging must not contain candidate PII.

The project rule is explicit: never log PII. These tests pin that rule at
the two places where the auth path could silently regress: the structured
auth logs and the LinkedIn upstream-response logs.
"""
import logging

import pytest

import services.linkedin_auth_service as linkedin_module
from core.config import AppSettings
from core.errors import BadRequestError, ConflictError, UnauthorizedError
from repositories.memory import InMemoryUserRepository
from services.auth_service import AuthService
from services.linkedin_auth_service import LinkedInAuthService

_EMAIL = "privacy.candidate@example.com"


def _logged_text(caplog) -> str:
    return "\n".join(
        f"{record.getMessage()} {record.__dict__}" for record in caplog.records
    )


@pytest.fixture
def auth_service():
    return AuthService(
        user_repository=InMemoryUserRepository(),
        settings=AppSettings(jwt_secret_key="test-secret-for-logging-at-least-32-bytes"),
    )


@pytest.mark.asyncio
async def test_signup_logs_do_not_include_the_email(auth_service, caplog):
    with caplog.at_level(logging.INFO):
        await auth_service.signup(_EMAIL, "password123", "candidate")

    assert _EMAIL not in _logged_text(caplog)

    caplog.clear()
    with caplog.at_level(logging.INFO):
        with pytest.raises(ConflictError):
            await auth_service.signup(_EMAIL, "password123", "candidate")

    assert _EMAIL not in _logged_text(caplog)


@pytest.mark.asyncio
async def test_login_logs_do_not_include_the_email(auth_service, caplog):
    await auth_service.signup(_EMAIL, "password123", "candidate")

    caplog.clear()
    with caplog.at_level(logging.INFO):
        await auth_service.login(_EMAIL, "password123")

    assert _EMAIL not in _logged_text(caplog)

    caplog.clear()
    with caplog.at_level(logging.INFO):
        with pytest.raises(UnauthorizedError):
            await auth_service.login("unknown.privacy@example.com", "password123")

    assert _EMAIL not in _logged_text(caplog)
    assert "unknown.privacy@example.com" not in _logged_text(caplog)


class _FakeResponse:
    def __init__(self, status_code: int, *, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeAsyncClient:
    """Minimal async context manager replacement for httpx.AsyncClient."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, *_args, **_kwargs):
        return _FakeResponse(200, payload={"access_token": "test-token"})

    async def get(self, *_args, **_kwargs):
        return _FakeResponse(
            500,
            text='{"email": "leaked.profile@example.com", "name": "Leaked Person"}',
        )


@pytest.mark.asyncio
async def test_linkedin_userinfo_error_does_not_log_the_response_body(monkeypatch, caplog):
    monkeypatch.setattr(linkedin_module.httpx, "AsyncClient", _FakeAsyncClient)
    service = LinkedInAuthService(
        AppSettings(
            environment="development",
            linkedin_client_id="client-id",
            linkedin_client_secret="client-secret",
        )
    )

    with caplog.at_level(logging.INFO):
        with pytest.raises(BadRequestError):
            await service.exchange_code_for_userinfo("authorization-code")

    logged = _logged_text(caplog)
    assert "leaked.profile@example.com" not in logged
    assert "Leaked Person" not in logged
    assert "500" in logged
