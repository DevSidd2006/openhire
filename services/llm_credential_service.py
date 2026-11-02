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
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from core.errors import BadRequestError
from core.logging import get_logger
from providers import get_llm_provider
from providers.base import LLMPermanentError, LLMProvider, LLMTransientError
from providers.llm import build_provider
from providers.llm.fallback import FallbackLLMProvider
from core.llm_context import reset_context_provider, set_context_provider
from repositories.interfaces import CredentialStatus, LLMCredentialRecord, LLMCredentialRepository
from services.provider_health import check_provider

logger = get_logger("services.llm_credential")


class _LazySystemProvider(LLMProvider):
    """Defers to `get_llm_provider()` at call time rather than at
    construction time, so the system-key fallback always reflects the
    live ContextVar/env state at the moment it is actually used (and so
    tests can patch `get_llm_provider` right up until the failing call)."""

    def __init__(self):
        self.last_usage = {}

    async def generate(self, prompt: str, **kwargs) -> str:
        token = set_context_provider(None)
        try:
            provider = get_llm_provider()
            result = await provider.generate(prompt, **kwargs)
            self.last_usage = getattr(provider, "last_usage", {})
            return result
        finally:
            reset_context_provider(token)

    async def generate_structured(self, prompt: str, schema, **kwargs):
        token = set_context_provider(None)
        try:
            provider = get_llm_provider()
            result = await provider.generate_structured(prompt, schema, **kwargs)
            self.last_usage = getattr(provider, "last_usage", {})
            return result
        finally:
            reset_context_provider(token)


def _platform_name() -> str:
    from config.settings import LLM_PROVIDER
    return f"platform:{LLM_PROVIDER}"


def _tracked_platform(repository, user_id: str) -> LLMProvider:
    provider_name = _platform_name()
    return _TrackedCredentialProvider(_LazySystemProvider(), repository, user_id, "platform", provider_name)


class _TrackedCredentialProvider(LLMProvider):
    def __init__(self, provider, repository, user_id: str, credential_id: str, provider_name: str):
        self.provider, self.repository = provider, repository
        self.user_id, self.credential_id, self.provider_name = user_id, credential_id, provider_name

    async def _record(self, outcome: str, input_tokens: int = 0, output_tokens: int = 0):
        try:
            await self.repository.record_usage(self.user_id, self.credential_id, self.provider_name, outcome, input_tokens, output_tokens)
        except Exception:
            # Usage persistence must never turn a successful AI result into a
            # failed interview or evaluation.
            return

    async def _run(self, method, prompt, *args, **kwargs):
        if hasattr(self.provider, "last_usage"):
            self.provider.last_usage = {}
        try:
            result = await getattr(self.provider, method)(prompt, *args, **kwargs)
        except Exception:
            await self._record("failure")
            raise
        usage = getattr(self.provider, "last_usage", None) or {}
        if not isinstance(usage, dict):
            usage = {}
        input_tokens = usage.get("input_tokens", max(1, len(prompt) // 4))
        output_tokens = usage.get("output_tokens", max(1, len(str(result)) // 4))
        await self._record("success", input_tokens, output_tokens)
        return result

    async def generate(self, prompt: str, **kwargs):
        return await self._run("generate", prompt, **kwargs)

    async def generate_structured(self, prompt: str, schema, **kwargs):
        return await self._run("generate_structured", prompt, schema, **kwargs)


class LLMCredentialService:
    """Use cases for BYOK credentials. One instance per request (cheap -
    holds only a repository reference and the process-wide encryption
    key), matching every other *Service in this backend."""

    def __init__(self, *, credential_repository: LLMCredentialRepository, encryption_key: str) -> None:
        self._credentials = credential_repository
        self._fernet = Fernet(encryption_key.encode("utf-8"))

    async def save(
        self, *, user_id: str, provider: str, api_key: str, model: Optional[str] = None,
        name: Optional[str] = None, credential_id: Optional[str] = None,
    ) -> LLMCredentialRecord:
        """Validate `api_key` with one cheap live call, then encrypt and
        upsert it. Raises BadRequestError (400) on an invalid key, writing
        nothing - a bad key must be caught here, not discovered later by a
        candidate mid-interview."""
        result = await check_provider(provider, api_key, model)
        if not result["connected"]:
            raise BadRequestError(
                f"Could not validate this {provider} API key: {result['message']}",
                internal_detail=f"BYOK validation failed for user_id={user_id!r} provider={provider!r} code={result['error_code']!r}",
            )

        encrypted_key = self._fernet.encrypt(api_key.encode("utf-8"))
        key_hint = f"...{api_key[-4:]}" if len(api_key) >= 4 else "...."

        existing = await self._credentials.get_by_id(user_id, credential_id) if credential_id else await self._credentials.get(user_id)
        record = LLMCredentialRecord(
            user_id=user_id,
            credential_id=credential_id or (existing.credential_id if existing else str(uuid4())),
            name=name or (existing.name if existing else "My API key"),
            provider=provider,
            model=model,
            encrypted_key=encrypted_key,
            key_hint=key_hint,
            status=CredentialStatus.ACTIVE,
            health_status="connected", auth_status="valid",
            last_tested_at=datetime.fromisoformat(result["last_tested_at"]),
            last_successful_test_at=datetime.fromisoformat(result["last_successful_test_at"]),
            health_error_code=None,
        )
        saved = await self._credentials.save(record)
        logger.info(
            "BYOK credential saved",
            extra={"event": "byok_credential_saved", "user_id": user_id, "provider": provider},
        )
        return saved

    async def get(self, user_id: str) -> Optional[LLMCredentialRecord]:
        return await self._credentials.get(user_id)

    async def list(self, user_id: str) -> list[LLMCredentialRecord]:
        return await self._credentials.list_for_user(user_id)

    async def get_by_id(self, user_id: str, credential_id: str) -> Optional[LLMCredentialRecord]:
        return await self._credentials.get_by_id(user_id, credential_id)

    async def update_metadata(self, user_id: str, credential_id: str, *, name=None, model=None, name_provided=False, model_provided=False):
        record = await self._credentials.get_by_id(user_id, credential_id)
        if record is None:
            return None
        model_value = model if model_provided else record.model
        health = None
        if model_provided and model_value != record.model:
            plaintext_key = self._fernet.decrypt(record.encrypted_key).decode("utf-8")
            health = await check_provider(record.provider, plaintext_key, model_value)
            if not health["connected"]:
                raise BadRequestError(f"Could not validate selected model: {health['message']}")
        updates = {
            "name": name if name_provided else record.name,
            "model": model if model_provided else record.model,
        }
        if health:
            checked_at = datetime.fromisoformat(health["last_tested_at"])
            updates.update(health_status="connected", auth_status="valid", last_tested_at=checked_at,
                           last_successful_test_at=checked_at, health_error_code=None, last_error=None, last_error_at=None,
                           status=CredentialStatus.ACTIVE)
        updated = record.model_copy(update=updates)
        return await self._credentials.save(updated)

    async def delete_key(self, user_id: str, credential_id: str) -> None:
        await self._credentials.delete_key(user_id, credential_id)
        settings = await self._credentials.get_settings(user_id)
        if settings.get("default_credential_id") == credential_id:
            remaining = await self._credentials.list_for_user(user_id)
            settings["default_credential_id"] = remaining[0].credential_id if remaining else None
            await self._credentials.save_settings(user_id, settings)

    async def get_settings(self, user_id: str) -> dict:
        return await self._credentials.get_settings(user_id)

    async def save_settings(self, user_id: str, settings: dict) -> dict:
        current_settings = await self._credentials.get_settings(user_id)
        credential_id = settings.get("default_credential_id")
        credential = None
        if credential_id:
            credential = await self._credentials.get_by_id(user_id, credential_id)
            if credential is None:
                raise BadRequestError("Default credential does not belong to this account.")
            provider = settings.get("provider")
            if provider and credential.provider != provider:
                raise BadRequestError("Default credential must match the selected provider.")
        if settings.get("mode") == "byok" and settings.get("model") and (
            settings.get("model") != current_settings.get("model")
            or settings.get("provider") != current_settings.get("provider")
            or credential_id != current_settings.get("default_credential_id")
        ):
            if credential is None:
                candidates = await self._credentials.list_for_user(user_id)
                provider = settings.get("provider")
                credential = next((row for row in candidates if row.provider == provider and row.status == CredentialStatus.ACTIVE), None)
            if credential is None:
                raise BadRequestError("Select a saved API key before enabling BYOK.")
            secret = self._fernet.decrypt(credential.encrypted_key).decode("utf-8")
            health = await check_provider(credential.provider, secret, settings["model"])
            if not health["connected"]:
                raise BadRequestError(f"Could not validate selected model: {health['message']}")
            checked_at = datetime.fromisoformat(health["last_tested_at"])
            await self._credentials.save(credential.model_copy(update={
                "model": settings["model"], "status": CredentialStatus.ACTIVE, "health_status": "connected",
                "auth_status": "valid", "last_tested_at": checked_at, "last_successful_test_at": checked_at,
                "health_error_code": None, "last_error": None, "last_error_at": None,
            }))
        return await self._credentials.save_settings(user_id, settings)

    async def get_usage(self, user_id: str) -> list[dict]:
        return await self._credentials.get_usage(user_id)

    async def save_platform_health(self, provider: str, result: dict) -> None:
        await self._credentials.save_platform_health(provider, result)

    async def get_platform_health(self) -> list[dict]:
        return await self._credentials.get_platform_health()

    async def delete(self, user_id: str) -> None:
        await self._credentials.delete(user_id)
        settings = await self._credentials.get_settings(user_id)
        settings["default_credential_id"] = None
        await self._credentials.save_settings(user_id, settings)
        logger.info(
            "BYOK credential deleted",
            extra={"event": "byok_credential_deleted", "user_id": user_id},
        )

    async def test_key(
        self, *, provider: str, api_key: str, model: Optional[str] = None
    ) -> dict:
        """Test auth and model access through provider metadata; no AI generation."""
        result = await check_provider(provider, api_key, model)
        result["success"] = result["connected"]
        result["message"] = ("API key is valid and the selected model is accessible." if result["connected"]
                              else f"Could not validate {provider} API key: {result.get('message') or 'provider check failed.'}")
        result["latency_ms"] = result.get("latency_ms", 0)
        return result

    async def test_saved(self, user_id: str, model: Optional[str] = None, credential_id: Optional[str] = None) -> dict:
        """Test the user's currently saved BYOK credential in place."""
        records = await self._credentials.list_for_user(user_id)
        if not records:
            raise BadRequestError("No saved API key found to test.")
        settings = await self._credentials.get_settings(user_id)
        record = next((r for r in records if r.credential_id == credential_id), None) if credential_id else next((r for r in records if r.credential_id == settings.get("default_credential_id")), records[0])
        if record is None:
            raise BadRequestError("No saved API key found to test.")

        plaintext_key = self._fernet.decrypt(record.encrypted_key).decode("utf-8")
        result = await self.test_key(
            provider=record.provider,
            api_key=plaintext_key,
            model=model or record.model,
        )
        result.setdefault("provider", record.provider)

        checked_at = datetime.fromisoformat(result["last_tested_at"]) if result.get("last_tested_at") else None
        if result["success"]:
            updated = record.model_copy(
                update={
                    "status": CredentialStatus.ACTIVE,
                    "last_error": None,
                    "last_error_at": None,
                    "health_status": result["status"], "auth_status": result["auth_status"],
                    "last_tested_at": checked_at, "last_successful_test_at": checked_at,
                    "health_error_code": None,
                }
            )
            await self._credentials.save(updated)
        elif not result["success"]:
            failed = record.model_copy(
                update={
                    "status": CredentialStatus.FAILED,
                    "last_error": result["message"],
                    "last_error_at": datetime.now(timezone.utc),
                    "health_status": result["status"], "auth_status": result["auth_status"],
                    "last_tested_at": checked_at, "health_error_code": result.get("error_code"),
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
        user_settings = await self._credentials.get_settings(user_id)
        if user_settings.get("mode") == "platform":
            return _tracked_platform(self._credentials, user_id)
        records = [
            record for record in await self._credentials.list_for_user(user_id)
            if record.status == CredentialStatus.ACTIVE
        ]
        if not records:
            return _tracked_platform(self._credentials, user_id)
        default_id = user_settings.get("default_credential_id")
        preferred_provider = user_settings.get("provider")
        selected = next((r for r in records if r.credential_id == default_id), None)
        if selected is not None and preferred_provider and selected.provider != preferred_provider:
            selected = None
        if selected is None and preferred_provider:
            selected = next((r for r in records if r.provider == preferred_provider), None)
        if selected is not None:
            records.sort(key=lambda r: (r.credential_id != selected.credential_id, r.created_at))
        elif records:
            records.sort(key=lambda r: (r.credential_id != default_id, r.created_at))
        selected_id = selected.credential_id if selected else default_id
        if not user_settings.get("fallback_to_other_keys", True):
            records = records[:1]
        chain = _tracked_platform(self._credentials, user_id) if user_settings.get("fallback_to_platform", True) else None
        for record in reversed(records):
            key = self._fernet.decrypt(record.encrypted_key).decode("utf-8")
            primary = _TrackedCredentialProvider(
                build_provider(
                    record.provider,
                    api_key=key,
                    model=(user_settings.get("model") if record.credential_id == selected_id else None) or record.model,
                ),
                self._credentials, user_id, record.credential_id, record.provider,
            )
            async def _on_primary_failure(exc: Exception, credential_id=record.credential_id, secret=key) -> None:
                current = await self._credentials.get_by_id(user_id, credential_id)
                if current:
                    failed = current.model_copy(update={
                        "status": CredentialStatus.FAILED, "last_error": str(exc).replace(secret, "[redacted]"),
                        "last_error_at": datetime.now(timezone.utc),
                    })
                    await self._credentials.save(failed)
            chain = FallbackLLMProvider(primary, chain, _on_primary_failure) if chain else primary
        return chain

    async def resolve_api_key_for_provider(self, user_id: str, provider: str) -> str | None:
        """Return an active saved key only when it belongs to ``provider``.

        This narrow internal method supports APIs, such as Gemini Live token
        minting, that need the provider credential rather than an
        :class:`LLMProvider` wrapper. The plaintext is never retained on this
        service or exposed by an API response.
        """
        settings = await self._credentials.get_settings(user_id)
        if settings.get("mode") == "platform":
            return None
        records = await self._credentials.list_for_user(user_id)
        default_id = settings.get("default_credential_id")
        record = next((r for r in records if r.credential_id == default_id and r.provider == provider and r.status == CredentialStatus.ACTIVE), None)
        if record is None:
            record = next((r for r in records if r.provider == provider and r.status == CredentialStatus.ACTIVE), None)
        if record is None:
            return None
        return self._fernet.decrypt(record.encrypted_key).decode("utf-8")
