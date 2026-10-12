"""
FastAPI application assembly.

This module builds the application through ``create_app()`` rather than
configuring a module-level singleton at import time.

``app = create_app()`` is still exported at module level because
``api.app:app`` is the documented Uvicorn target, deployment entry point,
and import target used by the tests.

Runnable in mock mode with no API key:

    python -m uvicorn api.app:app --reload

Starting the server does not call any LLM, speech, or vector provider.
Configuration is read from ``config/settings.py`` and ``core/config.py``,
while providers are resolved lazily at first use.

Wiring seams preserved
----------------------
The following application-state wiring hooks remain available and are read
by the dependency container on every request:

    app.state.registry
    app.state.interviewer_factory
    app.state.voice_service_factory
    app.state.jd_analyzer_factory
    app.state.resume_parser_factory
    app.state.resume_matcher_factory
    app.state.evaluation_agent_factories
    app.state.leaderboard_factory
"""

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from api.errors import register_exception_handlers
from api.models import HealthResponse
from api.routes.admin import router as admin_router
from api.routes.applications import router as applications_router
from api.routes.auth import router as auth_router
from api.routes.bugs import router as bugs_router
from api.routes.candidates import router as candidates_router
from api.routes.evaluations import router as evaluations_router
from api.routes.interview import router as interview_router
from api.routes.interview_mediator import router as interview_mediator_router
from api.routes.jobs import router as jobs_router
from api.routes.live_interview import router as live_interview_router
from api.routes.llm_credentials import router as llm_credentials_router
from api.routes.reports import router as reports_router
from api.routes.rubrics import router as rubrics_router
from api.routes.scoring import router as scoring_router
from api.routes.voice import router as voice_router
from core.config import AppSettings, get_settings
from core.container import ServiceContainer
from core.dependencies import get_container
from core.lifespan import build_lifespan
from core.middleware import install_middleware


# P9: The voice client is served from the API's own origin.
#
# ``pages/voice-interview.html`` builds its WebSocket URL from
# ``location.host``. Serving the page from the API keeps browser
# fetch/WebSocket requests same-origin.
#
# CORS remains available through CORS_ALLOW_ORIGINS for separately hosted
# frontends, but is disabled unless explicitly configured.
#
# StaticFiles confines requests to this directory, and there is no
# user-controlled path joined here.
_PAGES_DIR = Path(__file__).resolve().parent.parent / "pages"


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """Build and return a fully wired FastAPI application.

    Args:
        settings: Optional explicit application settings. When omitted,
            the process-wide cached settings are used.

    Passing explicit settings allows tests to construct differently
    configured applications without mutating the environment used by
    other tests.
    """
    from api.registry import SessionRegistry

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

    # ------------------------------------------------------------------
    # Application wiring seams
    # ------------------------------------------------------------------
    #
    # These are intentionally stored on app.state rather than copied into
    # the dependency container. This preserves test/deployment overrides
    # and allows the container to read the current values on each request.

    app.state.registry = SessionRegistry()

    # Existing interview/voice seams.
    app.state.interviewer_factory = None
    app.state.voice_service_factory = None
    app.state.gemini_live_token_factory = None

    # Chunk 2: job/candidate/matching agent seams.
    app.state.jd_analyzer_factory = None
    app.state.resume_parser_factory = None
    app.state.resume_matcher_factory = None

    # Chunk 4: evaluation-agent seam.
    app.state.evaluation_agent_factories = None

    # Chunk 5: leaderboard-agent seam.
    app.state.leaderboard_factory = None

    # Keep settings accessible through the application state.
    app.state.settings = settings

    # ------------------------------------------------------------------
    # Middleware and exception handling
    # ------------------------------------------------------------------

    install_middleware(app, settings)
    register_exception_handlers(app)

    # ------------------------------------------------------------------
    # API routers
    # ------------------------------------------------------------------

    app.include_router(auth_router, prefix=settings.api_prefix)
    app.include_router(admin_router, prefix=settings.api_prefix)
    app.include_router(interview_router, prefix=settings.api_prefix)
    app.include_router(voice_router, prefix=settings.api_prefix)
    app.include_router(live_interview_router, prefix=settings.api_prefix)
    app.include_router(
        interview_mediator_router,
        prefix=settings.api_prefix,
    )
    app.include_router(scoring_router, prefix=settings.api_prefix)
    app.include_router(reports_router, prefix=settings.api_prefix)
    app.include_router(jobs_router, prefix=settings.api_prefix)
    app.include_router(rubrics_router, prefix=settings.api_prefix)
    app.include_router(candidates_router, prefix=settings.api_prefix)
    app.include_router(applications_router, prefix=settings.api_prefix)
    app.include_router(evaluations_router, prefix=settings.api_prefix)
    app.include_router(bugs_router, prefix=settings.api_prefix)
    app.include_router(
        llm_credentials_router,
        prefix=settings.api_prefix,
    )

    # ------------------------------------------------------------------
    # Health endpoint
    # ------------------------------------------------------------------

    @app.api_route(
        f"{settings.api_prefix}/health",
        methods=["GET", "HEAD"],
        response_model=HealthResponse,
        tags=["health"],
    )
    async def health() -> HealthResponse:
        """Return a lightweight liveness response.

        This endpoint intentionally performs no LLM, provider, database,
        or upstream health checks. It is designed to be cheap enough for
        continuous load-balancer polling.

        The response body remains exactly:

            {"status": "ok"}
        """
        return HealthResponse(status="ok")

    # ------------------------------------------------------------------
    # Root operational summary
    # ------------------------------------------------------------------

    @app.api_route(
        f"{settings.api_prefix}/",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def root(
        container: ServiceContainer = Depends(get_container),
    ) -> dict:
        """Return a credential-free operational summary.

        ``public_summary()`` exposes provider names only and never exposes
        API keys, connection strings, or filesystem paths.

        Persistence is explicitly reported so an ephemeral deployment does
        not appear durable when its data is lost on restart.
        """
        return {
            "service": "openhire-live-interview-api",
            **container.settings.public_summary(),
            "persistence": (
                "ephemeral"
                if container.persistence_is_ephemeral
                else "durable"
            ),
        }

    # ------------------------------------------------------------------
    # Optional static frontend
    # ------------------------------------------------------------------

    if settings.serve_static_pages and _PAGES_DIR.is_dir():
        app.mount(
            "/app",
            StaticFiles(
                directory=str(_PAGES_DIR),
                html=True,
            ),
            name="pages",
        )

    return app


# Uvicorn/deployment/test entry point.
#
# Usage:
#
#     python -m uvicorn api.app:app --reload
#
# The public ``api.app:app`` contract remains unchanged.
app = create_app()
