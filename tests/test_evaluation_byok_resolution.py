"""Tests that EvaluationService resolves the owning recruiter's BYOK
provider explicitly (not via the ContextVar, which does not exist by the
time a background-scheduled evaluation runs — see core/llm_context.py's
docstring on 'the boundary, stated plainly')."""
from unittest.mock import AsyncMock, patch

import pytest

from repositories.interfaces import JobRecord
from repositories.memory import InMemoryJobRepository, InMemoryLLMCredentialRepository
from schemas.job import JobDescription
from services.llm_credential_service import LLMCredentialService

_ENCRYPTION_KEY = "IcVLp2xXjBdCQ8f5T7QYqGzq0m5ZKq8P4o5r5tqXQvI="


@pytest.mark.asyncio
async def test_resolve_llm_provider_for_job_returns_recruiter_credential_when_saved():
    from services.evaluation_service import _resolve_llm_provider_for_job

    job_repo = InMemoryJobRepository()
    await job_repo.save(JobRecord(
        job_id="job_1",
        job=JobDescription(job_id="job_1", title="t", description="d"),
        created_by_user_id="user_recruiter_1",
    ))
    credential_repo = InMemoryLLMCredentialRepository()
    credential_service = LLMCredentialService(
        credential_repository=credential_repo, encryption_key=_ENCRYPTION_KEY
    )
    with patch(
        "services.llm_credential_service.check_provider",
        new=AsyncMock(return_value={"provider":"openai", "model":"gpt-test", "connected":True, "status":"connected", "auth_status":"valid", "last_tested_at":"2026-11-02T00:00:00+00:00", "last_successful_test_at":"2026-11-02T00:00:00+00:00"}),
    ):
        await credential_service.save(
            user_id="user_recruiter_1", provider="openai", api_key="sk-realkey"
        )

    provider = await _resolve_llm_provider_for_job(
        job_id="job_1", job_repository=job_repo, llm_credential_service=credential_service
    )
    assert provider is not None


@pytest.mark.asyncio
async def test_resolve_llm_provider_for_job_preserves_platform_mode_when_recruiter_has_no_credential():
    from services.evaluation_service import _resolve_llm_provider_for_job

    job_repo = InMemoryJobRepository()
    await job_repo.save(JobRecord(
        job_id="job_2",
        job=JobDescription(job_id="job_2", title="t", description="d"),
        created_by_user_id="user_recruiter_2",
    ))
    credential_service = LLMCredentialService(
        credential_repository=InMemoryLLMCredentialRepository(), encryption_key=_ENCRYPTION_KEY
    )
    provider = await _resolve_llm_provider_for_job(
        job_id="job_2", job_repository=job_repo, llm_credential_service=credential_service
    )
    assert provider is not None


@pytest.mark.asyncio
async def test_resolve_llm_provider_for_job_returns_none_when_service_is_none():
    from services.evaluation_service import _resolve_llm_provider_for_job

    job_repo = InMemoryJobRepository()
    provider = await _resolve_llm_provider_for_job(
        job_id="job_3", job_repository=job_repo, llm_credential_service=None
    )
    assert provider is None


@pytest.mark.asyncio
async def test_resolve_llm_provider_for_job_returns_none_for_unknown_job():
    from services.evaluation_service import _resolve_llm_provider_for_job

    job_repo = InMemoryJobRepository()
    credential_service = LLMCredentialService(
        credential_repository=InMemoryLLMCredentialRepository(), encryption_key=_ENCRYPTION_KEY
    )
    provider = await _resolve_llm_provider_for_job(
        job_id="job_nonexistent", job_repository=job_repo, llm_credential_service=credential_service
    )
    assert provider is None
