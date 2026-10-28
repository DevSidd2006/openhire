"""
Cookie flag regressions for the LinkedIn OAuth flow.

Development runs over plain HTTP, so the auth cookies intentionally omit
Secure there. Production runs over TLS and must set it on every auth cookie
and on every matching deletion.
"""
from fastapi.testclient import TestClient

from api.routes.auth import get_linkedin_service
from core.config import AppSettings
from tests.test_linkedin_oauth import _build_test_app


class _FakeLinkedInService:
    def get_authorization_url(self, state: str) -> str:
        return f"https://www.linkedin.com/oauth/v2/authorization?state={state}"

    async def exchange_code_for_userinfo(self, _code: str) -> dict:
        return {
            "sub": "cookie-test-member",
            "email": "cookie.test@example.com",
            "name": "Cookie Test",
            "given_name": "Cookie",
            "family_name": "Test",
            "email_verified": True,
            "picture": "",
        }


def _set_cookie_headers(response) -> str:
    values = response.headers.get_list("set-cookie")
    return " ".join(values)


def _build_client(settings: AppSettings) -> TestClient:
    app, _, _ = _build_test_app(settings)
    app.dependency_overrides[get_linkedin_service] = lambda: _FakeLinkedInService()
    # No context manager: these tests exercise response headers and do not
    # need the production startup validation, which intentionally rejects
    # the in-memory repositories this test helper installs.
    return TestClient(app)


def _production_settings() -> AppSettings:
    return AppSettings(
        environment="production",
        debug=False,
        auth_enabled=True,
        jwt_secret_key="a-real-secret-from-the-environment",
        linkedin_client_id="live-client-id",
        linkedin_client_secret="live-client-secret",
        linkedin_mock_enabled=False,
    )


def _development_settings() -> AppSettings:
    return AppSettings(
        environment="development",
        linkedin_mock_enabled=True,
    )


def test_production_authorize_sets_secure_state_cookie():
    client = _build_client(_production_settings())

    response = client.get("/auth/linkedin/authorize?role=candidate", follow_redirects=False)

    assert response.status_code == 302
    raw = _set_cookie_headers(response)
    assert "oauth_state=" in raw
    assert "Secure" in raw


def test_production_callback_sets_secure_auth_cookies():
    client = _build_client(_production_settings())
    authorize = client.get("/auth/linkedin/authorize?role=candidate", follow_redirects=False)
    state = authorize.cookies["oauth_state"]

    callback = client.get(
        f"/auth/linkedin/callback?code=test-code&state={state}:candidate",
        headers={"Cookie": f"oauth_state={state}"},
        follow_redirects=False,
    )

    assert callback.status_code == 302
    raw = _set_cookie_headers(callback)
    assert "openhire_access_token=" in raw
    assert "openhire_oauth_ticket=" in raw
    assert raw.count("Secure") >= 3


def test_production_logout_deletes_secure_cookies():
    client = _build_client(_production_settings())

    response = client.post("/auth/logout")

    assert response.status_code == 200
    raw = _set_cookie_headers(response)
    for name in (
        "openhire_access_token",
        "openhire_refresh_token",
        "openhire_oauth_ticket",
        "oauth_state",
    ):
        assert name in raw
    assert raw.count("Secure") >= 4


def test_development_cookies_remain_usable_over_http():
    client = _build_client(_development_settings())

    authorize = client.get("/auth/linkedin/authorize?role=candidate", follow_redirects=False)
    assert "oauth_state=" in _set_cookie_headers(authorize)
    assert "Secure" not in _set_cookie_headers(authorize)

    state = authorize.cookies["oauth_state"]
    callback = client.get(
        f"/auth/linkedin/callback?code=test-code&state={state}:candidate",
        follow_redirects=False,
    )
    assert callback.status_code == 302
    raw = _set_cookie_headers(callback)
    assert "openhire_access_token=" in raw
    assert "Secure" not in raw
