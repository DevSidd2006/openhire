"""Request-lifecycle FastAPI dependencies — as opposed to core/dependencies.py,
which resolves *services*, this module wraps a request with setup/teardown
around another dependency. Currently holds exactly one: the BYOK ContextVar
seam.
"""
from __future__ import annotations

from fastapi import Depends

from core.config import AppSettings, get_settings
from core.container import ServiceContainer
from core.dependencies import get_container
from core.llm_context import reset_context_provider, set_context_provider
from core.security import Principal, PrincipalType, require_authenticated


async def resolve_candidate_practice_llm_provider(
    *, principal: Principal, job_id: str, candidate_id: str,
    application_id: str | None, settings: AppSettings, container,
    jobs, candidate_service,
):
    """Resolve candidate BYOK only for the owner's candidate-created mock job."""
    if not (
        settings.auth_enabled and settings.byok_encryption_key
        and principal.principal_type is PrincipalType.CANDIDATE
        and principal.subject_id and application_id is None
    ):
        return None
    from core.errors import NotFoundError

    try:
        job = await jobs.get_job(job_id)
    except NotFoundError:
        return None
    if not job.is_practice or job.created_by_user_id != principal.subject_id:
        return None
    await candidate_service.validate_candidate_ownership(candidate_id, principal.subject_id)
    from services.llm_credential_service import LLMCredentialService

    service = LLMCredentialService(
        credential_repository=container.llm_credential_repository,
        encryption_key=settings.byok_encryption_key,
    )
    return await service.resolve_provider_for_user(principal.subject_id)


async def apply_llm_context_for_recruiter(
    principal: Principal = Depends(require_authenticated),
    settings: AppSettings = Depends(get_settings),
    container: ServiceContainer = Depends(get_container),
):
    """Attach to any recruiter-authenticated route that makes a synchronous
    LLM call (e.g. POST /jobs, which runs JDAnalyzerAgent inline) so
    get_llm_provider() resolves that recruiter's BYOK credential for the
    duration of this request.

    Inert unless: AUTH_ENABLED and BYOK_ENCRYPTION_KEY are both set, AND the
    caller is a recruiter or admin (a candidate is never looked up - see
    spec 'Scope: who can use BYOK'). In every other case this is a no-op,
    matching the rest of the auth boundary's fail-inert-not-fail-closed
    posture while a feature is off.

    Uses a yield-dependency specifically so the ContextVar is guaranteed to
    be reset even if the route handler raises - a provider must never
    outlive the request that set it (core/llm_context.py's docstring).
    """
    token = None
    should_resolve = (
        settings.auth_enabled
        and settings.byok_encryption_key
        and principal.principal_type in (PrincipalType.RECRUITER, PrincipalType.ADMIN)
        and principal.subject_id is not None
    )
    if should_resolve:
        from services.llm_credential_service import LLMCredentialService

        service = LLMCredentialService(
            credential_repository=container.llm_credential_repository,
            encryption_key=settings.byok_encryption_key,
        )
        provider = await service.resolve_provider_for_user(principal.subject_id)
        if provider is not None:
            token = set_context_provider(provider)
    try:
        yield
    finally:
        if token is not None:
            reset_context_provider(token)
