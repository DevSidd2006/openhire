"""BYOK endpoints: GET/PUT/DELETE /auth/me/llm-credential.

Mounted at /auth/me (not a separate /llm-credentials prefix) to sit
alongside the existing GET/PATCH /auth/me and POST /auth/me/password -
all three are "manage my own account" actions (api/routes/auth.py).

Gated on settings.byok_encryption_key being set: with BYOK off, every
endpoint here returns 404 as if it does not exist, rather than a 500 from
get_llm_credential_service's defensive ValueError - the profile page never
calls these endpoints while BYOK is off (Task 14), and an operator probing
the API sees the same "not found" a disabled feature should show.
"""
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, Response

from api.models_llm_credentials import (
    LLMCredentialResponse,
    SaveLLMCredentialRequest,
    TestLLMCredentialRequest,
    TestLLMCredentialResponse,
    LLMCredentialSettingsRequest,
    LLMCredentialMetadataRequest,
)
from core.config import AppSettings, get_settings
from core.dependencies import get_llm_credential_service
from core.errors import ForbiddenError, NotFoundError
from core.security import Principal, PrincipalType, require_authenticated
from services.llm_credential_service import LLMCredentialService
from services.provider_health import PROVIDERS, check_provider, platform_credentials

router = APIRouter(prefix="/auth/me", tags=["byok"])


def _require_byok_enabled(settings: AppSettings = Depends(get_settings)) -> None:
    if not settings.byok_encryption_key:
        raise NotFoundError("BYOK is not enabled on this deployment.")


async def _require_recruiter(principal: Principal = Depends(require_authenticated)) -> Principal:
    if principal.principal_type not in (PrincipalType.CANDIDATE, PrincipalType.RECRUITER, PrincipalType.ADMIN):
        raise ForbiddenError("BYOK is available to signed-in accounts.")
    return principal


@router.get("/llm-provider-health", dependencies=[Depends(_require_byok_enabled)])
async def get_provider_health(service: LLMCredentialService = Depends(get_llm_credential_service), principal: Principal = Depends(_require_recruiter)):
    """Health view scoped to this account; platform keys are only reported as status."""
    credentials = await service.list(principal.subject_id)
    settings = await service.get_settings(principal.subject_id)
    rows = []
    for record in credentials:
        rows.append({"provider": record.provider, "credential_id": record.credential_id, "name": record.name,
                     "model": record.model or "provider default", "status": record.health_status,
                     "auth_status": record.auth_status, "connected": record.health_status == "connected",
                     "last_tested_at": record.last_tested_at.isoformat() if record.last_tested_at else None,
                     "last_successful_test_at": record.last_successful_test_at.isoformat() if record.last_successful_test_at else None,
                     "error_code": record.health_error_code, "message": record.last_error})
    configured = platform_credentials()
    saved_platform_health = {row["provider"]: row for row in await service.get_platform_health()}
    for provider in PROVIDERS:
        key, model, _ = configured[provider]
        checked = saved_platform_health.get(provider, {})
        last_test = checked.get("last_tested_at")
        last_success = checked.get("last_successful_test_at")
        rows.append({"provider": provider, "credential_id": "platform", "name": "OpenHire Platform",
                     "model": checked.get("model") or model, "status": checked.get("status") or ("not_tested" if key else "not_configured"),
                     "auth_status": checked.get("auth_status") or ("not_tested" if key else "not_configured"), "connected": checked.get("status") == "connected",
                     "last_tested_at": last_test.isoformat() if hasattr(last_test, "isoformat") else last_test,
                     "last_successful_test_at": last_success.isoformat() if hasattr(last_success, "isoformat") else last_success,
                     "error_code": checked.get("error_code"),
                     "message": checked.get("message") or ("Test connection to verify the platform key." if key else "No platform key configured.")})
    return {"mode": settings.get("mode", "platform"), "providers": rows}


@router.post("/llm-provider-health/test", dependencies=[Depends(_require_byok_enabled)])
async def test_provider_health(payload: TestLLMCredentialRequest, service: LLMCredentialService = Depends(get_llm_credential_service), principal: Principal = Depends(_require_recruiter)):
    payload.validate_provider()
    if payload.credential_id == "platform":
        key, model, _ = platform_credentials()[payload.provider or ""]
        result = await check_provider(payload.provider or "", key, payload.model or model)
        await service.save_platform_health(payload.provider or "", result)
    elif payload.credential_id:
        result = await service.test_saved(principal.subject_id, payload.model, payload.credential_id)
    elif payload.api_key:
        result = await service.test_key(provider=payload.provider or "", api_key=payload.api_key, model=payload.model)
    else:
        key, model, _ = platform_credentials()[payload.provider or ""]
        result = await check_provider(payload.provider or "", key, payload.model or model)
    return result


@router.get("/llm-credentials", dependencies=[Depends(_require_byok_enabled)])
async def list_llm_credentials(service: LLMCredentialService = Depends(get_llm_credential_service), principal: Principal = Depends(_require_recruiter)):
    return [LLMCredentialResponse.from_domain(r) for r in await service.list(principal.subject_id)]


@router.post("/llm-credentials", response_model=LLMCredentialResponse, dependencies=[Depends(_require_byok_enabled)])
async def create_llm_credential(payload: SaveLLMCredentialRequest, service: LLMCredentialService = Depends(get_llm_credential_service), principal: Principal = Depends(_require_recruiter)):
    payload.validate_provider()
    record = await service.save(user_id=principal.subject_id, credential_id=str(uuid4()), name=payload.name or "My API key", provider=payload.provider, api_key=payload.api_key, model=payload.model)
    settings = await service.get_settings(principal.subject_id)
    if not settings.get("default_credential_id"):
        settings["default_credential_id"] = record.credential_id
        await service.save_settings(principal.subject_id, settings)
    return LLMCredentialResponse.from_domain(record)


@router.patch("/llm-credentials/{credential_id}", response_model=LLMCredentialResponse, dependencies=[Depends(_require_byok_enabled)])
async def update_llm_credential(credential_id: str, payload: LLMCredentialMetadataRequest, service: LLMCredentialService = Depends(get_llm_credential_service), principal: Principal = Depends(_require_recruiter)):
    current = await service.get_by_id(principal.subject_id, credential_id)
    if current is None:
        raise NotFoundError("Credential not found.")
    if payload.api_key:
        payload.validate_provider()
        record = await service.save(
            user_id=principal.subject_id, credential_id=credential_id,
            name=payload.name if payload.name is not None else current.name,
            provider=payload.provider or current.provider, api_key=payload.api_key,
            model=payload.model if "model" in payload.model_fields_set else current.model,
        )
        return LLMCredentialResponse.from_domain(record)
    record = await service.update_metadata(principal.subject_id, credential_id, name=payload.name, model=payload.model, name_provided="name" in payload.model_fields_set, model_provided="model" in payload.model_fields_set)
    if record is None:
        raise NotFoundError("Credential not found.")
    return LLMCredentialResponse.from_domain(record)


@router.delete("/llm-credentials/{credential_id}", status_code=204, dependencies=[Depends(_require_byok_enabled)])
async def delete_llm_credential_by_id(credential_id: str, service: LLMCredentialService = Depends(get_llm_credential_service), principal: Principal = Depends(_require_recruiter)):
    if not await service.get_by_id(principal.subject_id, credential_id):
        raise NotFoundError("Credential not found.")
    await service.delete_key(principal.subject_id, credential_id)


@router.get("/llm-settings", dependencies=[Depends(_require_byok_enabled)])
async def get_llm_settings(service: LLMCredentialService = Depends(get_llm_credential_service), principal: Principal = Depends(_require_recruiter)):
    return await service.get_settings(principal.subject_id)


@router.put("/llm-settings", dependencies=[Depends(_require_byok_enabled)])
async def put_llm_settings(payload: LLMCredentialSettingsRequest, service: LLMCredentialService = Depends(get_llm_credential_service), principal: Principal = Depends(_require_recruiter)):
    payload.validate_mode()
    return await service.save_settings(principal.subject_id, payload.model_dump())


@router.get("/llm-usage", dependencies=[Depends(_require_byok_enabled)])
async def get_llm_usage(service: LLMCredentialService = Depends(get_llm_credential_service), principal: Principal = Depends(_require_recruiter)):
    return await service.get_usage(principal.subject_id)


@router.get("/llm-credential", response_model=None, dependencies=[Depends(_require_byok_enabled)])
async def get_llm_credential(
    service: LLMCredentialService = Depends(get_llm_credential_service),
    principal: Principal = Depends(_require_recruiter),
):
    """GET /auth/me/llm-credential. 204 (no body) when none is saved -
    never a 404, since "no credential saved" is the normal, expected state
    for most users, not an error."""
    record = await service.get(principal.subject_id)
    if record is None:
        return Response(status_code=204)
    return LLMCredentialResponse.from_domain(record)


@router.put("/llm-credential", response_model=LLMCredentialResponse, dependencies=[Depends(_require_byok_enabled)])
async def save_llm_credential(
    payload: SaveLLMCredentialRequest,
    service: LLMCredentialService = Depends(get_llm_credential_service),
    principal: Principal = Depends(_require_recruiter),
) -> LLMCredentialResponse:
    """PUT /auth/me/llm-credential. Validates the key with one live call
    before persisting (400 on failure, writes nothing - see
    services/llm_credential_service.py:LLMCredentialService.save)."""
    payload.validate_provider()
    record = await service.save(
        user_id=principal.subject_id,
        provider=payload.provider,
        api_key=payload.api_key,
        model=payload.model,
    )
    return LLMCredentialResponse.from_domain(record)


@router.delete("/llm-credential", status_code=204, dependencies=[Depends(_require_byok_enabled)])
async def delete_llm_credential(
    service: LLMCredentialService = Depends(get_llm_credential_service),
    principal: Principal = Depends(_require_recruiter),
) -> None:
    """DELETE /auth/me/llm-credential. Idempotent - removing an already-absent
    credential still returns 204."""
    await service.delete(principal.subject_id)


@router.post("/llm-credential/test", response_model=TestLLMCredentialResponse, dependencies=[Depends(_require_byok_enabled)])
async def test_llm_credential(
    payload: Optional[TestLLMCredentialRequest] = Body(None),
    service: LLMCredentialService = Depends(get_llm_credential_service),
    principal: Principal = Depends(_require_recruiter),
) -> TestLLMCredentialResponse:
    """POST /auth/me/llm-credential/test. Tests an entered API key in place
    without saving, or tests the user's currently saved credential if no key
    is provided in the payload."""
    if payload and payload.api_key and payload.api_key.strip():
        payload.validate_provider()
        res = await service.test_key(
            provider=payload.provider or "gemini",
            api_key=payload.api_key.strip(),
            model=payload.model,
        )
        res.setdefault("provider", payload.provider or "gemini")
        res.setdefault("message", f"Could not validate {payload.provider or 'gemini'} API key." if not res.get("success") else "API key is valid and the selected model is accessible.")
        return TestLLMCredentialResponse(**res)

    res = await service.test_saved(
        user_id=principal.subject_id,
        model=payload.model if payload else None,
        credential_id=payload.credential_id if payload else None,
    )
    res.setdefault("message", "API key is valid and the selected model is accessible." if res.get("success") else "Provider connection failed.")
    return TestLLMCredentialResponse(**res)
