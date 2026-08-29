"""
FastAPI application assembly.

This module now builds the application through `create_app()` rather than
configuring a module-level singleton at import time. Import-time
construction meant there was no point at which the app could be built with
different settings, no shutdown hook, and no way to stand up a second
isolated instance in a test.

`app = create_app()` is still exported at module level, because
`api.app:app` is the documented uvicorn target, the deployment entry point,
and what `tests/test_api.py` and `tests/test_voice_layer.py` import. That
contract is unchanged.

Runnable in mock mode with no API key:

    python -m uvicorn api.app:app --reload

Starting the server calls no LLM, speech or vector provider. Configuration
is read (config/settings.py for provider/domain values, core/config.py for
service values) and validated, and providers are resolved lazily at first
use, exactly as before.

Wiring seams preserved verbatim
-------------------------------
`app.state.registry`, `app.state.interviewer_factory` and
`app.state.voice_service_factory` remain exactly what they were: pure wiring
hooks, None/default in a real deployment, read on every request. The
dependency container (core/container.py) reads through to them rather than
shadowing them, so assigning them still works.
"""
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from api.errors import register_exception_handlers
from api.models import HealthResponse
from api.registry import SessionRegistry
from api.routes.applications import router as applications_router
from api.routes.candidates import router as candidates_router
from api.routes.evaluations import router as evaluations_router
from api.routes.interview import router as interview_router
from api.routes.jobs import router as jobs_router
from api.routes.voice import router as voice_router
from core.config import AppSettings, get_settings
from core.container import ServiceContainer
from core.dependencies import get_container
from core.lifespan import build_lifespan
from core.middleware import install_middleware

# P9: the voice client is served from the API's OWN origin.
#
# Necessary, not cosmetic: pages/voice-interview.html builds its WebSocket
# URL from `location.host`, so opening it as a file:// URL yields an empty
# host and the socket can never connect. Serving it here also keeps the
# browser's fetch/WebSocket same-origin, which is why the vertical slice
# needs no CORS configuration (CORS is now available via CORS_ALLOW_ORIGINS
# for a separately-hosted frontend - see core/config.py - but is off unless
# an operator opts in). Read-only static hosting of one directory: no upload
# path and no user-supplied path is ever joined here, so it introduces no
# traversal surface of its own (StaticFiles normalizes and confines paths
# beneath the mounted directory).
_PAGES_DIR = Path(__file__).resolve().parent.parent / "pages"


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """Build a fully wired application.

    `settings` defaults to the process-wide cached settings; passing an
    explicit object is what lets a test exercise a differently-configured
    app (production mode, CORS enabled, auth enabled) without mutating the
    environment for every other test in the session.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title=settings.app_name,
        description=(
            "Thin HTTP transport over the existing adaptive interview engine "
            "(InterviewSessionRunner). Contains no interview intelligence of "
            "its own - see utils/interview_session.py for the source of truth."
        ),
        version=settings.app_version,
        lifespan=build_lifespan(settings),
        docs_url=settings.docs_url,
        redoc_url=None,
        openapi_url=settings.openapi_url,
    )

    # The three pre-existing wiring seams. See the module docstring; the
    # container reads these on every request rather than copying them.
    app.state.registry = SessionRegistry()
    app.state.interviewer_factory = None
    app.state.voice_service_factory = None
    # Chunk 2: the same kind of pure wiring seam, for the three agents the
    # job/candidate/matching services wrap (JDAnalyzerAgent,
    # ResumeParserAgent, ResumeMatcherAgent). None means "let the agent
    # resolve its own default LLM provider" - see core/container.py's
    # `*_factory_for` methods.
    app.state.jd_analyzer_factory = None
    app.state.resume_parser_factory = None
    app.state.resume_matcher_factory = None
    # Chunk 4: one bundled seam for the seven evaluation agents - see
    # core/container.py:evaluation_agent_factories_for and
    # services/evaluation_service.py:EvaluationAgentFactories.
    app.state.evaluation_agent_factories = None
    # Chunk 5: same pattern, for LeaderboardAgent (RecruiterService).
    app.state.leaderboard_factory = None
    app.state.settings = settings

    install_middleware(app, settings)
    register_exception_handlers(app)

    app.include_router(interview_router, prefix=settings.api_prefix)
    app.include_router(voice_router, prefix=settings.api_prefix)
    app.include_router(jobs_router, prefix=settings.api_prefix)
    app.include_router(candidates_router, prefix=settings.api_prefix)
    app.include_router(applications_router, prefix=settings.api_prefix)
    app.include_router(evaluations_router, prefix=settings.api_prefix)

    @app.api_route(
        f"{settings.api_prefix}/health",
        methods=["GET", "HEAD"],
        response_model=HealthResponse,
        tags=["health"],
    )
    async def health() -> HealthResponse:
        """Liveness. No LLM call, no provider call, no database call, no API
        key required (P5 Phase 13).

        The body is exactly `{"status": "ok"}` and stays that way. A liveness
        probe is polled continuously by a load balancer, so it must be the
        cheapest endpoint in the service and must not describe the service's
        internals to whoever can reach it. Operational detail lives on `/`
        instead (below).

        It deliberately does not probe upstreams either: a health check that
        fails because a third-party model is briefly unavailable takes the
        whole service out of rotation for something it can still partly
        serve, and burns paid quota on every probe.
        """
        return HealthResponse(status="ok")

    @app.api_route(f"{settings.api_prefix}/", methods=["GET", "HEAD"], include_in_schema=False)
    async def root(container: ServiceContainer = Depends(get_container)) -> dict:
        """How this process is configured.

        Credential-free by construction: `AppSettings.public_summary()`
        returns provider *names* only - never a key, a connection string or a
        path. `persistence` is here specifically so a deployment still running
        on the temporary in-memory repositories announces it, rather than
        looking healthy while losing every transcript on restart.
        """
        return {
            "service": "openhire-live-interview-api",
            **container.settings.public_summary(),
            "persistence": "ephemeral" if container.persistence_is_ephemeral else "durable",
        }

    if settings.serve_static_pages and _PAGES_DIR.is_dir():
        app.mount("/app", StaticFiles(directory=str(_PAGES_DIR), html=True), name="pages")

    return app


# The uvicorn/deployment/test entry point. Unchanged contract.
app = create_app()
