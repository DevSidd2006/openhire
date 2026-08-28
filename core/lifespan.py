"""
Application startup and shutdown.

Before this, the application had no lifecycle at all: `api/app.py` built a
registry at import time and there was nothing that ran on shutdown. That is
survivable while every dependency is an in-process dict and stops being
survivable the moment a connection pool exists - which is precisely what the
database chunk will add.

Startup does four things, in this order:

  1. Configure logging, so that everything after it is logged consistently.
  2. Validate configuration and fail fast on anything that would otherwise
     surface as a confusing error on the first real request.
  3. Build the dependency container.
  4. Log a credential-free startup banner describing how the service is
     actually configured.

Shutdown releases the container.

Fail-fast, and what it deliberately does NOT check
--------------------------------------------------
Startup validates configuration; it does not make network calls. It will
not call the LLM provider, the speech service or (later) the database to
"verify connectivity". A health probe that requires an upstream to be
reachable turns a transient provider outage into a service that cannot
start at all, and burns paid API quota on every deploy. The existing
`/health` endpoint keeps its documented property of making no provider call.
"""
from __future__ import annotations

import contextlib
from typing import AsyncIterator

from fastapi import FastAPI

from core.config import AppSettings
from core.container import ServiceContainer, build_default_container
from core.errors import ConfigurationError
from core.logging import configure_logging, get_logger
from core.security import AnonymousAuthProvider

logger = get_logger("core.lifespan")

# Providers that need no credential. Anything else selected in a
# non-development environment must have had its credential validated by the
# provider factory it belongs to; this only catches the config-level cases
# that are cheap and unambiguous to check here.
_CREDENTIAL_FREE_PROVIDERS = {"mock"}


def validate_startup_configuration(
    settings: AppSettings, container: ServiceContainer
) -> None:
    """Refuse to start on a configuration that cannot work as described.

    Each check below corresponds to a way the service could otherwise run
    while quietly not doing what its configuration claims - the failure mode
    worth spending a startup check on.
    """
    problems: list[str] = []

    # "Auth is enabled" must not be a claim the service cannot honour.
    if settings.auth_enabled and isinstance(container.auth_provider, AnonymousAuthProvider):
        problems.append(
            "AUTH_ENABLED=true but no AuthProvider is installed - the service "
            "would authenticate every request as anonymous while reporting "
            "that authentication is on. Install a real AuthProvider in "
            "core/container.py, or set AUTH_ENABLED=false."
        )

    # Ephemeral persistence in production is data loss, not a degraded mode.
    if settings.is_production and container.persistence_is_ephemeral:
        problems.append(
            "ENVIRONMENT=production with in-memory persistence - interview "
            "records and sealed transcripts would be lost on every restart. "
            "Install the database-backed repositories in core/container.py."
        )

    if problems:
        raise ConfigurationError(
            internal_detail="; ".join(problems),
            context={"environment": settings.environment},
        )

    # Warnings, not failures: these are legitimate in development.
    if settings.llm_provider in _CREDENTIAL_FREE_PROVIDERS and settings.is_production:
        logger.warning(
            "LLM_PROVIDER=%s in a production environment - interviews will be "
            "driven by deterministic mock output, not a real model.",
            settings.llm_provider,
        )
    if container.persistence_is_ephemeral:
        logger.warning(
            "persistence is EPHEMERAL - see repositories/memory.py. Session "
            "records and sealed transcripts do not survive a restart."
        )


def build_lifespan(settings: AppSettings):
    """Build the ASGI lifespan handler for an app configured with `settings`.

    A factory rather than a bare lifespan function so that an app built with
    non-default settings (a test, an embedded instance) gets a lifespan that
    agrees with them, instead of one that re-reads the global cache.
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings)

        container = build_default_container(settings)
        validate_startup_configuration(settings, container)
        app.state.container = container

        logger.info(
            "startup complete: %s",
            settings.public_summary(),
            extra={"event": "startup", **settings.public_summary(),
                   "persistence": "ephemeral" if container.persistence_is_ephemeral else "durable"},
        )

        try:
            yield
        finally:
            # Shutdown must complete even if a component's close fails, so
            # that the remaining components are still released - aclose()
            # isolates each one internally.
            logger.info("shutdown: releasing application dependencies",
                        extra={"event": "shutdown"})
            await container.aclose()

    return lifespan


__all__ = ["build_lifespan", "validate_startup_configuration"]
