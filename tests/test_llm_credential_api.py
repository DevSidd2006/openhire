"""Tests for POST/GET/DELETE /auth/me/llm-credential (BYOK)."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.errors import register_exception_handlers
from api.routes.llm_credentials import router as llm_credentials_router
from core.config import AppSettings, get_settings
from core.container import ServiceContainer
from core.security import JWTAuthProvider
from providers.base import LLMPermanentError
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

_ENCRYPTION_KEY = "IcVLp2xXjBdCQ8f5T7QYqGzq0m5ZKq8P4o5r5tqXQvI="  # a fixed valid Fernet key, for test determinism


def _app():
    settings = AppSettings(auth_enabled=True, byok_encryption_key=_ENCRYPTION_KEY)
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
    app = FastAPI()
    app.state.container = container
    register_exception_handlers(app)
    app.include_router(llm_credentials_router)
    app.dependency_overrides[get_settings] = lambda: settings
    return app, auth_service


@pytest.fixture
def client_and_auth():
    app, auth_service = _app()
    return TestClient(app), auth_service


def _mock_validation_ok():
    return patch(
        "services.llm_credential_service.check_provider",
        new=AsyncMock(return_value={"connected": True, "status": "connected", "auth_status": "valid", "last_tested_at": "2026-11-02T00:00:00+00:00", "last_successful_test_at": "2026-11-02T00:00:00+00:00"}),
    )


def _mock_validation_fails():
    return patch(
        "services.llm_credential_service.check_provider",
        new=AsyncMock(return_value={"connected": False, "status": "invalid_api_key", "auth_status": "invalid", "error_code": "invalid_api_key", "message": "bad key"}),
    )


@pytest.mark.asyncio
async def test_get_with_no_credential_returns_204(client_and_auth):
    client, auth_service = client_and_auth
    _user, token, _r = await auth_service.signup(
        email="recruiter@example.com", password="password123", user_type="recruiter"
    )
    response = client.get("/auth/me/llm-credential", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_recruiter_can_save_credential(client_and_auth):
    client, auth_service = client_and_auth
    _user, token, _r = await auth_service.signup(
        email="recruiter@example.com", password="password123", user_type="recruiter"
    )
    with _mock_validation_ok():
        response = client.put(
            "/auth/me/llm-credential",
            json={"provider": "gemini", "api_key": "sk-test", "model": "gemini-flash-lite-latest"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert response.json()["provider"] == "gemini"


@pytest.mark.asyncio
async def test_unauthenticated_gets_401(client_and_auth):
    client, _auth_service = client_and_auth
    response = client.get("/auth/me/llm-credential")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_candidate_can_manage_only_their_own_credentials(client_and_auth):
    client, auth_service = client_and_auth
    _user, token, _r = await auth_service.signup(
        email="candidate@example.com", password="password123", user_type="candidate"
    )
    headers = {"Authorization": f"Bearer {token}"}
    with _mock_validation_ok():
        response = client.post("/auth/me/llm-credentials", json={"name":"Practice key","provider":"openai","api_key":"candidate-secret"}, headers=headers)
    assert response.status_code == 200
    assert client.get("/auth/me/llm-credentials", headers=headers).json()[0]["name"] == "Practice key"
    assert "candidate-secret" not in client.get("/auth/me/llm-credentials", headers=headers).text
    credential_id = client.get("/auth/me/llm-credentials", headers=headers).json()[0]["credential_id"]
    preferences = {"mode":"platform","default_credential_id":credential_id,"fallback_to_other_keys":False,"fallback_to_platform":True,"model":"gpt-4o-mini"}
    assert client.put("/auth/me/llm-settings", json=preferences, headers=headers).json()["mode"] == "platform"
    assert client.get("/auth/me/llm-settings", headers=headers).json()["default_credential_id"] == credential_id
    _user, other_token, _ = await auth_service.signup(email="another-candidate@example.com", password="password123", user_type="candidate")
    other_headers = {"Authorization": f"Bearer {other_token}"}
    assert client.get("/auth/me/llm-credentials", headers=other_headers).json() == []
    assert client.get("/auth/me/llm-settings", headers=other_headers).json()["mode"] == "platform"


@pytest.mark.asyncio
async def test_multi_key_settings_edit_and_ownership_endpoints(client_and_auth):
    client, auth_service = client_and_auth
    _user, owner_token, _ = await auth_service.signup(email="owner@example.com", password="password123", user_type="recruiter")
    _user, other_token, _ = await auth_service.signup(email="other@example.com", password="password123", user_type="recruiter")
    owner = {"Authorization": f"Bearer {owner_token}"}
    other = {"Authorization": f"Bearer {other_token}"}
    with _mock_validation_ok():
        first = client.post("/auth/me/llm-credentials", json={"name":"OpenAI primary","provider":"openai","api_key":"key-one"}, headers=owner)
        second = client.post("/auth/me/llm-credentials", json={"name":"OpenAI backup","provider":"openai","api_key":"key-two"}, headers=owner)
    assert first.status_code == 200 and second.status_code == 200
    assert len(client.get("/auth/me/llm-credentials", headers=owner).json()) == 2
    key_id = first.json()["credential_id"]
    with _mock_validation_ok():
        update = client.patch(f"/auth/me/llm-credentials/{key_id}", json={"name":"Renamed","model":"gpt-4o-mini"}, headers=owner)
    assert update.json()["name"] == "Renamed"
    assert client.patch(f"/auth/me/llm-credentials/{key_id}", json={"name":"Intrusion"}, headers=other).status_code == 404
    config = {"mode":"platform","default_credential_id":key_id,"fallback_to_other_keys":False,"fallback_to_platform":True}
    assert client.put("/auth/me/llm-settings", json=config, headers=owner).json()["mode"] == "platform"
    assert client.get("/auth/me/llm-settings", headers=other).json()["mode"] == "platform"
    assert client.delete(f"/auth/me/llm-credentials/{key_id}", headers=owner).status_code == 204
    assert len(client.get("/auth/me/llm-credentials", headers=owner).json()) == 1


@pytest.mark.asyncio
async def test_recruiter_save_then_get_never_echoes_the_key(client_and_auth):
    client, auth_service = client_and_auth
    _user, token, _r = await auth_service.signup(
        email="recruiter@example.com", password="password123", user_type="recruiter"
    )
    with _mock_validation_ok():
        put_response = client.put(
            "/auth/me/llm-credential",
            json={"provider": "gemini", "api_key": "sk-realkey123", "model": "gemini-2.5-flash"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert put_response.status_code == 200, put_response.text
    body = put_response.json()
    assert body["provider"] == "gemini"
    assert body["key_hint"] == "...y123"
    assert "api_key" not in body
    assert "sk-realkey123" not in put_response.text

    get_response = client.get("/auth/me/llm-credential", headers={"Authorization": f"Bearer {token}"})
    assert get_response.status_code == 200
    assert get_response.json()["key_hint"] == "...y123"
    assert "sk-realkey123" not in get_response.text


@pytest.mark.asyncio
async def test_invalid_key_returns_400_and_writes_nothing(client_and_auth):
    client, auth_service = client_and_auth
    _user, token, _r = await auth_service.signup(
        email="recruiter@example.com", password="password123", user_type="recruiter"
    )
    with _mock_validation_fails():
        response = client.put(
            "/auth/me/llm-credential",
            json={"provider": "gemini", "api_key": "sk-badkey", "model": "gemini-2.5-flash"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 400

    get_response = client.get("/auth/me/llm-credential", headers={"Authorization": f"Bearer {token}"})
    assert get_response.status_code == 204


@pytest.mark.asyncio
async def test_delete_removes_the_credential(client_and_auth):
    client, auth_service = client_and_auth
    _user, token, _r = await auth_service.signup(
        email="recruiter@example.com", password="password123", user_type="recruiter"
    )
    with _mock_validation_ok():
        client.put(
            "/auth/me/llm-credential",
            json={"provider": "gemini", "api_key": "sk-realkey123", "model": "gemini-2.5-flash"},
            headers={"Authorization": f"Bearer {token}"},
        )
    delete_response = client.delete("/auth/me/llm-credential", headers={"Authorization": f"Bearer {token}"})
    assert delete_response.status_code == 204

    get_response = client.get("/auth/me/llm-credential", headers={"Authorization": f"Bearer {token}"})
    assert get_response.status_code == 204


@pytest.mark.asyncio
async def test_test_key_in_place_success_does_not_persist(client_and_auth):
    client, auth_service = client_and_auth
    _user, token, _r = await auth_service.signup(
        email="recruiter@example.com", password="password123", user_type="recruiter"
    )
    with _mock_validation_ok():
        response = client.post(
            "/auth/me/llm-credential/test",
            json={"provider": "gemini", "api_key": "sk-test123", "model": "gemini-flash-lite-latest"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["provider"] == "gemini"
    assert "latency_ms" in data
    assert "model is accessible" in data["message"]

    # Key was NOT persisted
    get_res = client.get("/auth/me/llm-credential", headers={"Authorization": f"Bearer {token}"})
    assert get_res.status_code == 204


@pytest.mark.asyncio
async def test_test_key_in_place_failure_returns_success_false(client_and_auth):
    client, auth_service = client_and_auth
    _user, token, _r = await auth_service.signup(
        email="recruiter@example.com", password="password123", user_type="recruiter"
    )
    with _mock_validation_fails():
        response = client.post(
            "/auth/me/llm-credential/test",
            json={"provider": "gemini", "api_key": "sk-bad123", "model": "gemini-flash-lite-latest"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False
    assert "bad key" in data["message"]


@pytest.mark.asyncio
async def test_test_key_unsupported_provider_returns_400(client_and_auth):
    client, auth_service = client_and_auth
    _user, token, _r = await auth_service.signup(
        email="recruiter@example.com", password="password123", user_type="recruiter"
    )
    response = client.post(
        "/auth/me/llm-credential/test",
        json={"provider": "unsupported_provider", "api_key": "sk-123"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_test_saved_key_success(client_and_auth):
    client, auth_service = client_and_auth
    _user, token, _r = await auth_service.signup(
        email="recruiter@example.com", password="password123", user_type="recruiter"
    )
    with _mock_validation_ok():
        # First save a key
        client.put(
            "/auth/me/llm-credential",
            json={"provider": "gemini", "api_key": "sk-saved123", "model": "gemini-flash-lite-latest"},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Test saved key with empty body
        response = client.post(
            "/auth/me/llm-credential/test",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["provider"] == "gemini"


@pytest.mark.asyncio
async def test_test_saved_key_when_none_saved_returns_400(client_and_auth):
    client, auth_service = client_and_auth
    _user, token, _r = await auth_service.signup(
        email="recruiter@example.com", password="password123", user_type="recruiter"
    )
    response = client.post(
        "/auth/me/llm-credential/test",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400
    assert "No saved API key found" in response.json()["detail"]


@pytest.mark.asyncio
async def test_test_key_unauthenticated_returns_401(client_and_auth):
    client, _auth_service = client_and_auth
    response = client.post("/auth/me/llm-credential/test", json={"api_key": "sk-test"})
    assert response.status_code == 401
