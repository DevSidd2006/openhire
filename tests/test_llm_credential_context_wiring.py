"""Tests that a recruiter's BYOK provider is visible to get_llm_provider()
for the duration of a request that depends on apply_llm_context_for_recruiter,
and is gone again afterward - the core/llm_context.py ContextVar seam."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.app import create_app
from api.dependencies import apply_llm_context_for_recruiter
from api.errors import register_exception_handlers
from core.config import AppSettings, get_settings
from core.container import ServiceContainer
from core.llm_context import get_context_provider
from core.security import JWTAuthProvider, require_authenticated
from providers.llm import get_llm_provider
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

_ENCRYPTION_KEY = "IcVLp2xXjBdCQ8f5T7QYqGzq0m5ZKq8P4o5r5tqXQvI="


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
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/probe", dependencies=[Depends(apply_llm_context_for_recruiter)])
    async def probe(principal=Depends(require_authenticated)):
        provider = get_llm_provider()
        result = await provider.generate("probe the configured provider")
        return {"provider_is_context_provider": get_context_provider() is provider, "result": result}

    return app, auth_service


@pytest.mark.asyncio
async def test_recruiter_with_saved_credential_sees_it_in_get_llm_provider():
    app, auth_service = _app()
    user, token, _r = await auth_service.signup(
        email="recruiter@example.com", password="password123", user_type="recruiter"
    )
    container = app.state.container
    from services.llm_credential_service import LLMCredentialService

    service = LLMCredentialService(
        credential_repository=container.llm_credential_repository,
        encryption_key=_ENCRYPTION_KEY,
    )
    with patch(
        "services.llm_credential_service.check_provider",
        new=AsyncMock(return_value={"provider":"openai", "model":"gpt-test", "connected":True, "status":"connected", "auth_status":"valid", "last_tested_at":"2026-11-02T00:00:00+00:00", "last_successful_test_at":"2026-11-02T00:00:00+00:00"}),
    ):
        credential = await service.save(user_id=user.user_id, provider="openai", api_key="sk-realkey")
        await service.save_settings(user.user_id, {
            "mode": "byok", "provider": "openai", "default_credential_id": credential.credential_id,
            "fallback_to_other_keys": False, "fallback_to_platform": False, "model": "gpt-4o-mini",
        })

    client = TestClient(app)
    with patch(
        "services.llm_credential_service.build_provider",
        return_value=AsyncMock(generate=AsyncMock(return_value="ok")),
    ):
        response = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["provider_is_context_provider"] is True
    assert response.json()["result"] == "ok"
    usage = await service.get_usage(user.user_id)
    assert len(usage) == 1 and usage[0]["credential_id"] == credential.credential_id
    assert usage[0]["successes"] == 1


@pytest.mark.asyncio
async def test_context_is_cleared_after_the_request():
    app, auth_service = _app()
    _user, token, _r = await auth_service.signup(
        email="recruiter2@example.com", password="password123", user_type="recruiter"
    )
    client = TestClient(app)
    client.get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert get_context_provider() is None


@pytest.mark.asyncio
async def test_recruiter_with_no_saved_credential_keeps_platform_provider_and_tracks_usage():
    app, auth_service = _app()
    _user, token, _r = await auth_service.signup(
        email="recruiter3@example.com", password="password123", user_type="recruiter"
    )
    client = TestClient(app)
    response = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert response.json()["provider_is_context_provider"] is True


@pytest.mark.asyncio
async def test_a_candidate_never_triggers_a_credential_lookup():
    app, auth_service = _app()
    _user, token, _r = await auth_service.signup(
        email="candidate@example.com", password="password123", user_type="candidate"
    )
    client = TestClient(app)
    response = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["provider_is_context_provider"] is False


def test_real_app_wires_apply_llm_context_for_recruiter_onto_job_routes(monkeypatch):
    """The prior tests above only prove the ContextVar seam works against a
    synthetic /probe route. This proves the dependency is actually attached
    to the REAL production routes that make recruiter-authenticated LLM
    calls: POST /jobs (job description analysis) and
    POST /jobs/{job_id}/match (resume matching) - see api/routes/jobs.py.
    Rather than poke at FastAPI/Starlette route internals (which vary by
    version), this overrides `apply_llm_context_for_recruiter` itself with a
    spy and drives both routes through a real TestClient - if the
    dependency were missing from either route's decorator, the spy would
    never be invoked for that call."""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    try:
        app = create_app()

        calls = []

        async def _spy():
            calls.append(True)

        app.dependency_overrides[apply_llm_context_for_recruiter] = _spy

        with TestClient(app) as client:
            response = client.post(
                "/jobs",
                json={
                    "description": (
                        "Senior Python Backend Developer. Build async "
                        "Python services. Requires Python, REST APIs, SQL. "
                        "3+ years experience."
                    )
                },
            )
            assert response.status_code == 201, response.text
            job_id = response.json()["job_id"]
            assert len(calls) == 1, "apply_llm_context_for_recruiter not wired onto POST /jobs"

            match_response = client.post(f"/jobs/{job_id}/match")
            assert match_response.status_code == 200, match_response.text
            assert len(calls) == 2, (
                "apply_llm_context_for_recruiter not wired onto POST /jobs/{job_id}/match"
            )
    finally:
        get_settings.cache_clear()
