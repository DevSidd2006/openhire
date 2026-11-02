from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.dependencies import resolve_candidate_practice_llm_provider
from core.config import AppSettings
from core.security import Principal, PrincipalType

_KEY = "IcVLp2xXjBdCQ8f5T7QYqGzq0m5ZKq8P4o5r5tqXQvI="


@pytest.mark.asyncio
async def test_candidate_byok_resolves_only_for_owned_practice_jobs(monkeypatch):
    provider = object()
    resolved = AsyncMock(return_value=provider)
    monkeypatch.setattr("services.llm_credential_service.LLMCredentialService.resolve_provider_for_user", resolved)
    jobs = SimpleNamespace(get_job=AsyncMock(return_value=SimpleNamespace(is_practice=True, created_by_user_id="candidate-user")))
    candidates = SimpleNamespace(validate_candidate_ownership=AsyncMock())
    container = SimpleNamespace(llm_credential_repository=object())
    settings = AppSettings(auth_enabled=True, byok_encryption_key=_KEY)
    candidate = Principal(principal_type=PrincipalType.CANDIDATE, subject_id="candidate-user")

    result = await resolve_candidate_practice_llm_provider(
        principal=candidate, job_id="practice-job", candidate_id="candidate-profile",
        application_id=None, settings=settings, container=container,
        jobs=jobs, candidate_service=candidates,
    )
    assert result is provider
    candidates.validate_candidate_ownership.assert_awaited_once_with("candidate-profile", "candidate-user")
    resolved.assert_awaited_once_with("candidate-user")


@pytest.mark.asyncio
@pytest.mark.parametrize("practice,owner,application_id", [
    (False, "candidate-user", None),
    (True, "another-user", None),
    (True, "candidate-user", "real-application"),
])
async def test_candidate_byok_is_not_resolved_for_real_or_other_users_jobs(monkeypatch, practice, owner, application_id):
    resolved = AsyncMock()
    monkeypatch.setattr("services.llm_credential_service.LLMCredentialService.resolve_provider_for_user", resolved)
    jobs = SimpleNamespace(get_job=AsyncMock(return_value=SimpleNamespace(is_practice=practice, created_by_user_id=owner)))
    candidates = SimpleNamespace(validate_candidate_ownership=AsyncMock())
    candidate = Principal(principal_type=PrincipalType.CANDIDATE, subject_id="candidate-user")

    result = await resolve_candidate_practice_llm_provider(
        principal=candidate, job_id="job", candidate_id="profile",
        application_id=application_id, settings=AppSettings(auth_enabled=True, byok_encryption_key=_KEY),
        container=SimpleNamespace(llm_credential_repository=object()), jobs=jobs,
        candidate_service=candidates,
    )
    assert result is None
    resolved.assert_not_awaited()
    candidates.validate_candidate_ownership.assert_not_awaited()
