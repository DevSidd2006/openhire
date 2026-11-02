"""BYOK API request/response models. Deliberately do NOT nest
LLMCredentialRecord the way api/models_bugs.py nests BugReportRecord: that
record carries `encrypted_key`, which must never appear in a response.
"""
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from core.errors import BadRequestError
from repositories.interfaces import CredentialStatus, LLMCredentialRecord

_SUPPORTED_PROVIDERS = {"openai", "gemini", "anthropic", "groq", "nvidia_nim", "openrouter"}


class SaveLLMCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    credential_id: Optional[str] = None
    model: Optional[str] = None

    def validate_provider(self) -> None:
        if self.provider not in _SUPPORTED_PROVIDERS:
            raise BadRequestError(
                f"Unsupported provider {self.provider!r}. "
                f"Supported: {', '.join(sorted(_SUPPORTED_PROVIDERS))}."
            )


class LLMCredentialResponse(BaseModel):
    credential_id: str
    name: str
    provider: str
    model: Optional[str]
    key_hint: str
    status: CredentialStatus
    last_error: Optional[str]
    last_error_at: Optional[str]
    health_status: str = "not_tested"
    auth_status: str = "unknown"
    last_tested_at: Optional[str] = None
    last_successful_test_at: Optional[str] = None
    health_error_code: Optional[str] = None

    @classmethod
    def from_domain(cls, record: LLMCredentialRecord) -> "LLMCredentialResponse":
        return cls(
            credential_id=record.credential_id,
            name=record.name,
            provider=record.provider,
            model=record.model,
            key_hint=record.key_hint,
            status=record.status,
            last_error=record.last_error,
            last_error_at=record.last_error_at.isoformat() if record.last_error_at else None,
            health_status=record.health_status, auth_status=record.auth_status,
            last_tested_at=record.last_tested_at.isoformat() if record.last_tested_at else None,
            last_successful_test_at=record.last_successful_test_at.isoformat() if record.last_successful_test_at else None,
            health_error_code=record.health_error_code,
        )


class TestLLMCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Optional[str] = "gemini"
    api_key: Optional[str] = None
    model: Optional[str] = None
    credential_id: Optional[str] = None

    def validate_provider(self) -> None:
        if self.provider and self.provider not in _SUPPORTED_PROVIDERS:
            raise BadRequestError(
                f"Unsupported provider {self.provider!r}. "
                f"Supported: {', '.join(sorted(_SUPPORTED_PROVIDERS))}."
            )


class TestLLMCredentialResponse(BaseModel):
    success: bool
    provider: str
    model: Optional[str] = None
    latency_ms: int
    message: str


class LLMCredentialSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str
    default_credential_id: Optional[str] = None
    provider: Optional[str] = None
    fallback_to_other_keys: bool = True
    fallback_to_platform: bool = True
    model: Optional[str] = None

    def validate_mode(self) -> None:
        if self.mode not in {"byok", "platform"}:
            raise BadRequestError("Mode must be 'byok' or 'platform'.")
        if self.provider and self.provider not in _SUPPORTED_PROVIDERS:
            raise BadRequestError(
                f"Unsupported provider {self.provider!r}. "
                f"Supported: {', '.join(sorted(_SUPPORTED_PROVIDERS))}."
            )


class LLMCredentialMetadataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    model: Optional[str] = None
    provider: Optional[str] = None
    api_key: Optional[str] = Field(default=None, min_length=1)

    def validate_provider(self) -> None:
        if self.provider and self.provider not in _SUPPORTED_PROVIDERS:
            raise BadRequestError(f"Unsupported provider {self.provider!r}.")


__all__ = [
    "LLMCredentialResponse",
    "SaveLLMCredentialRequest",
    "TestLLMCredentialRequest",
    "TestLLMCredentialResponse",
    "LLMCredentialSettingsRequest",
    "LLMCredentialMetadataRequest",
]
