"""Tests for LLMCredentialService: encryption round-trip, no-leak, live-call
validation on save, and provider resolution (with fallback wiring)."""
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet

from core.errors import BadRequestError
from providers.base import LLMPermanentError
from repositories.interfaces import CredentialStatus
from repositories.memory import InMemoryLLMCredentialRepository
from services.llm_credential_service import LLMCredentialService

_KEY = Fernet.generate_key().decode()


def _service(repo=None):
    return LLMCredentialService(
        credential_repository=repo or InMemoryLLMCredentialRepository(),
        encryption_key=_KEY,
    )


@pytest.mark.asyncio
async def test_save_encrypts_the_key_and_round_trips():
    repo = InMemoryLLMCredentialRepository()
    service = _service(repo)
    with patch("services.llm_credential_service.check_provider", new=AsyncMock(return_value={"connected": True, "status": "connected", "auth_status": "valid", "last_tested_at": "2026-11-02T00:00:00+00:00", "last_successful_test_at": "2026-11-02T00:00:00+00:00"})):
        saved = await service.save(user_id="user_1", provider="openai", api_key="sk-real-key")

    assert saved.encrypted_key != b"sk-real-key"
    assert b"sk-real-key" not in saved.encrypted_key
    stored = await repo.get("user_1")
    assert stored.encrypted_key == saved.encrypted_key


@pytest.mark.asyncio
async def test_encrypting_the_same_key_twice_produces_different_ciphertext():
    """Fernet includes a random nonce and timestamp per encryption - two
    saves of the identical plaintext must not be comparable by ciphertext."""
    service = _service()
    with patch("services.llm_credential_service.check_provider", new=AsyncMock(return_value={"connected": True, "status": "connected", "auth_status": "valid", "last_tested_at": "2026-11-02T00:00:00+00:00", "last_successful_test_at": "2026-11-02T00:00:00+00:00"})):
        first = await service.save(user_id="user_1", provider="openai", api_key="sk-same-key")
        second = await service.save(user_id="user_2", provider="openai", api_key="sk-same-key")

    assert first.encrypted_key != second.encrypted_key


@pytest.mark.asyncio
async def test_save_computes_a_key_hint_from_the_last_four_chars():
    service = _service()
    with patch("services.llm_credential_service.check_provider", new=AsyncMock(return_value={"connected": True, "status": "connected", "auth_status": "valid", "last_tested_at": "2026-11-02T00:00:00+00:00", "last_successful_test_at": "2026-11-02T00:00:00+00:00"})):
        saved = await service.save(user_id="user_1", provider="openai", api_key="sk-abcd1234")

    assert saved.key_hint == "...1234"


@pytest.mark.asyncio
async def test_save_validates_with_a_live_call_and_rejects_a_bad_key():
    service = _service()
    with patch("services.llm_credential_service.check_provider", new=AsyncMock(return_value={"connected": False, "status": "invalid_api_key", "auth_status": "invalid", "error_code": "invalid_api_key", "message": "invalid api key"})):
        with pytest.raises(BadRequestError):
            await service.save(user_id="user_1", provider="openai", api_key="sk-bad-key")


@pytest.mark.asyncio
async def test_a_rejected_save_writes_nothing():
    repo = InMemoryLLMCredentialRepository()
    service = _service(repo)
    with patch("services.llm_credential_service.check_provider", new=AsyncMock(return_value={"connected": False, "status": "invalid_api_key", "auth_status": "invalid", "error_code": "invalid_api_key", "message": "invalid api key"})):
        with pytest.raises(BadRequestError):
            await service.save(user_id="user_1", provider="openai", api_key="sk-bad-key")

    assert await repo.get("user_1") is None


@pytest.mark.asyncio
async def test_decryption_under_a_wrong_master_key_fails_loudly():
    repo = InMemoryLLMCredentialRepository()
    service_a = _service(repo)
    with patch("services.llm_credential_service.check_provider", new=AsyncMock(return_value={"connected": True, "status": "connected", "auth_status": "valid", "last_tested_at": "2026-11-02T00:00:00+00:00", "last_successful_test_at": "2026-11-02T00:00:00+00:00"})):
        await service_a.save(user_id="user_1", provider="openai", api_key="sk-real-key")
        await service_a.save_settings("user_1", {"mode": "byok", "default_credential_id": None, "provider": "openai", "fallback_to_other_keys": True, "fallback_to_platform": True, "model": None})

    other_key = Fernet.generate_key().decode()
    service_b = LLMCredentialService(credential_repository=repo, encryption_key=other_key)
    with pytest.raises(Exception):
        await service_b.resolve_provider_for_user("user_1")


@pytest.mark.asyncio
async def test_resolve_provider_for_user_uses_tracked_platform_when_no_credential_saved():
    service = _service()
    assert await service.resolve_provider_for_user("nobody") is not None


@pytest.mark.asyncio
async def test_resolve_provider_for_user_returns_a_fallback_wrapped_provider():
    from providers.llm.fallback import FallbackLLMProvider

    repo = InMemoryLLMCredentialRepository()
    service = _service(repo)
    with patch("services.llm_credential_service.check_provider", new=AsyncMock(return_value={"connected": True, "status": "connected", "auth_status": "valid", "last_tested_at": "2026-11-02T00:00:00+00:00", "last_successful_test_at": "2026-11-02T00:00:00+00:00"})):
        await service.save(user_id="user_1", provider="openai", api_key="sk-real-key")
        await service.save_settings("user_1", {"mode": "byok", "default_credential_id": None, "provider": "openai", "fallback_to_other_keys": True, "fallback_to_platform": True, "model": None})

    resolved = await service.resolve_provider_for_user("user_1")
    assert isinstance(resolved, FallbackLLMProvider)


@pytest.mark.asyncio
async def test_a_primary_failure_marks_the_credential_status_failed():
    repo = InMemoryLLMCredentialRepository()
    service = _service(repo)
    with patch("services.llm_credential_service.check_provider", new=AsyncMock(return_value={"connected": True, "status": "connected", "auth_status": "valid", "last_tested_at": "2026-11-02T00:00:00+00:00", "last_successful_test_at": "2026-11-02T00:00:00+00:00"})):
        await service.save(user_id="user_1", provider="openai", api_key="sk-real-key")
        await service.save_settings("user_1", {"mode": "byok", "default_credential_id": None, "provider": "openai", "fallback_to_other_keys": True, "fallback_to_platform": True, "model": None})

    resolved = await service.resolve_provider_for_user("user_1")
    # Simulate a subsequent call failing (the mock_build provider set above
    # only answers the validation call at save-time; resolve_provider_for_user
    # builds a NEW provider instance from the decrypted key for actual use).
    with patch("services.llm_credential_service.build_provider") as mock_build_2:
        broken_provider = AsyncMock()
        broken_provider.generate = AsyncMock(side_effect=LLMPermanentError("revoked"))
        mock_build_2.return_value = broken_provider
        resolved = await service.resolve_provider_for_user("user_1")
        with patch("services.llm_credential_service.get_llm_provider") as mock_system:
            system_provider = AsyncMock()
            system_provider.generate = AsyncMock(return_value="from system key")
            mock_system.return_value = system_provider
            result = await resolved.generate("hi")
            assert result == "from system key"

    stored = await repo.get("user_1")
    assert stored.status == CredentialStatus.FAILED
    assert stored.last_error is not None
