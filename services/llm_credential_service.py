"""BYOK use cases: saving, retrieving, deleting, and resolving a user's LLM
credential (docs/superpowers/specs/2026-09-06-byok-design.md).

The plaintext key exists in exactly three places, per the spec: the HTTPS
request body on save, a local variable during encryption (`save`, below),
and a local variable during provider construction
(`resolve_provider_for_user`, below). It is never assigned to a field on a
long-lived object, never included in a response model, and never logged.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from core.errors import BadRequestError
from core.logging import get_logger
from providers import get_llm_provider
from providers.base import LLMPermanentError, LLMProvider, LLMTransientError
from providers.llm import build_provider
from providers.llm.fallback import FallbackLLMProvider
from repositories.interfaces import CredentialStatus, LLMCredentialRecord, LLMCredentialRepository

logger = get_logger("services.llm_credential")


class _LazySystemProvider(LLMProvider):
    """Defers to `get_llm_provider()` at call time rather than at
    construction time, so the system-key fallback always reflects the
    live ContextVar/env state at the moment it is actually used (and so
    tests can patch `get_llm_provider` right up until the failing call)."""

    async def generate(self, prompt: str, **kwargs) -> str:
        return await get_llm_provider().generate(prompt, **kwargs)

    async def generate_structured(self, prompt: str, schema, **kwargs):
        return await get_llm_provider().generate_structured(prompt, schema, **kwargs)


class LLMCredentialService:
    """Use cases for BYOK credentials. One instance per request (cheap -
    holds only a repository reference and the process-wide encryption
    key), matching every other *Service in this backend."""

    def __init__(self, *, credential_repository: LLMCredentialRepository, encryption_key: str) -> None:
        self._credentials = credential_repository
        self._fernet = Fernet(encryption_key.encode("utf-8"))

    async def save(
        self, *, user_id: str, provider: str, api_key: str, model: Optional[str] = None
    ) -> LLMCredentialRecord:
        """Validate `api_key` with one cheap live call, then encrypt and
        upsert it. Raises BadRequestError (400) on an invalid key, writing
        nothing - a bad key must be caught here, not discovered later by a
        candidate mid-interview."""
        test_provider = build_provider(provider, api_key=api_key, model=model)
        try:
            await test_provider.generate("Reply with the single word: ok", max_tokens=20)
        except (LLMPermanentError, LLMTransientError) as e:
            raise BadRequestError(
                f"Could not validate this {provider} API key: {e}",
                internal_detail=f"BYOK validation failed for user_id={user_id!r} provider={provider!r}: {e}",
            ) from e

        encrypted_key = self._fernet.encrypt(api_key.encode("utf-8"))
        key_hint = f"...{api_key[-4:]}" if len(api_key) >= 4 else "...."

        record = LLMCredentialRecord(
            user_id=user_id,
            provider=provider,
            model=model,
            encrypted_key=encrypted_key,
            key_hint=key_hint,
            status=CredentialStatus.ACTIVE,
        )
        saved = await self._credentials.save(record)
        logger.info(
            "BYOK credential saved",
            extra={"event": "byok_credential_saved", "user_id": user_id, "provider": provider},
        )
        return saved

    async def get(self, user_id: str) -> Optional[LLMCredentialRecord]:
        return await self._credentials.get(user_id)

    async def delete(self, user_id: str) -> None:
        await self._credentials.delete(user_id)
        logger.info(
            "BYOK credential deleted",
            extra={"event": "byok_credential_deleted", "user_id": user_id},
        )

    async def test_key(
        self, *, provider: str, api_key: str, model: Optional[str] = None
    ) -> dict:
        """Test an API key by making a minimal generation call in place, measuring latency."""
        test_provider = build_provider(provider, api_key=api_key, model=model)
        start_time = time.monotonic()
        try:
            await test_provider.generate("Reply with the single word: ok", max_tokens=20)
            elapsed_ms = max(1, int((time.monotonic() - start_time) * 1000))
            resolved_model = model or getattr(test_provider, "model", None)
            return {
                "success": True,
                "provider": provider,
                "model": resolved_model,
                "latency_ms": elapsed_ms,
                "message": f"API key is valid and working ({elapsed_ms}ms).",
            }
        except Exception as e:
            elapsed_ms = max(1, int((time.monotonic() - start_time) * 1000))
            resolved_model = model or getattr(test_provider, "model", None)
            logger.warning(
                "BYOK test key failed",
                extra={"event": "byok_test_failed", "provider": provider, "error": str(e)},
            )
            return {
                "success": False,
                "provider": provider,
                "model": resolved_model,
                "latency_ms": elapsed_ms,
                "message": f"Could not validate {provider} API key: {e}",
            }

    async def test_saved(self, user_id: str, model: Optional[str] = None) -> dict:
        """Test the user's currently saved BYOK credential in place."""
        record = await self._credentials.get(user_id)
        if record is None:
            raise BadRequestError("No saved API key found to test.")

        plaintext_key = self._fernet.decrypt(record.encrypted_key).decode("utf-8")
        result = await self.test_key(
            provider=record.provider,
            api_key=plaintext_key,
            model=model or record.model,
        )

        if result["success"] and record.status == CredentialStatus.FAILED:
            updated = record.model_copy(
                update={
                    "status": CredentialStatus.ACTIVE,
                    "last_error": None,
                    "last_error_at": None,
                }
            )
            await self._credentials.save(updated)
        elif not result["success"]:
            failed = record.model_copy(
                update={
                    "status": CredentialStatus.FAILED,
                    "last_error": result["message"],
                    "last_error_at": datetime.now(timezone.utc),
                }
            )
            await self._credentials.save(failed)

        return result

    async def resolve_provider_for_user(self, user_id: str) -> Optional[LLMProvider]:
        """The provider to use for calls made on `user_id`'s behalf: their
        BYOK provider wrapped in FallbackLLMProvider (falls back to the
        system key on failure and marks the credential status='failed'),
        or None if they have no saved credential (caller should then use
        get_llm_provider() unchanged).

        Raises if the stored ciphertext cannot be decrypted under this
        service's encryption_key (e.g. BYOK_ENCRYPTION_KEY was rotated) -
        this is a configuration failure, not a per-user one, so it is not
        swallowed here (the spec's "decryption under a wrong master key
        fails loudly" testing requirement).
        """
        record = await self._credentials.get(user_id)
        if record is None:
            return None

        plaintext_key = self._fernet.decrypt(record.encrypted_key).decode("utf-8")
        primary = build_provider(record.provider, api_key=plaintext_key, model=record.model)

        async def _on_primary_failure(exc: Exception) -> None:
            current = await self._credentials.get(user_id)
            if current is None:
                # The user deleted their credential while this call was in
                # flight - nothing to mark failed anymore, and re-saving the
                # stale `record` snapshot would resurrect a deleted row.
                return
            failed = current.model_copy(
                update={
                    "status": CredentialStatus.FAILED,
                    "last_error": str(exc),
                    "last_error_at": datetime.now(timezone.utc),
                }
            )
            await self._credentials.save(failed)
            logger.warning(
                "BYOK credential failed, falling back to system key",
                extra={"event": "byok_credential_failed", "user_id": user_id, "error": str(exc)},
            )

        return FallbackLLMProvider(primary, _LazySystemProvider(), _on_primary_failure)

    async def resolve_api_key_for_provider(self, user_id: str, provider: str) -> str | None:
        """Return an active saved key only when it belongs to ``provider``.

        This narrow internal method supports APIs, such as Gemini Live token
        minting, that need the provider credential rather than an
        :class:`LLMProvider` wrapper. The plaintext is never retained on this
        service or exposed by an API response.
        """
        record = await self._credentials.get(user_id)
        if (
            record is None
            or record.provider != provider
            or record.status != CredentialStatus.ACTIVE
        ):
            return None
        return self._fernet.decrypt(record.encrypted_key).decode("utf-8")
