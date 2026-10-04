"""
FastAPI application assembly.

This module builds the application through `create_app()` rather than
configuring a module-level singleton at import time.

`app = create_app()` is still exported at module level because
`api.app:app` is the documented uvicorn target, the deployment entry point,
and what `tests/test_api.py` and `tests/test_voice_layer.py` import.

Runnable in mock mode with no API key:

    python -m uvicorn api.app:app --reload

Starting the server calls no LLM, speech, or vector provider.
Configuration is read from:

- config/settings.py for provider/domain values
- core/config.py for service values

Providers are resolved lazily at first use.

Wiring seams preserved:

- `app.state.registry`
- `app.state.interviewer_factory`
- `app.state.voice_service_factory`

These remain pure wiring hooks and are read on every request.
The dependency container (`core/container.py`) reads through to them
rather than shadowing them.
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
# `pages/voice-interview.html` builds its WebSocket URL from
# `location.host`. Opening the page directly as a `file://` URL would
# result in an empty host, preventing the WebSocket connection.
#
# Serving the page from the API also keeps browser fetch/WebSocket
# requests same-origin, so this vertical slice does not require CORS.
#
# CORS is available through `CORS_ALLOW_ORIGINS` in `core/config.py`
# for deployments using a separately hosted frontend.
#
# StaticFiles provides read-only static hosting and confines access
# beneath the mounted directory.
_PAGES_DIR = Path(__file__).resolve().parent.parent / "pages"


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """
    Build and return a fully wired FastAPI application.

    If `settings` is not supplied, the process-wide cached settings
    are used.

    Passing an explicit settings object allows tests to create an
    independently configured application without modifying environment
    variables for the rest of the test session.
    """

    from api.registry import SessionRegistry

    if settings is None:
        settings = get_settings()

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
    # Pre-existing wiring seams
    # ------------------------------------------------------------------

    app.state.registry = SessionRegistry()
    app.state.interviewer_factory = None
    app.state.voice_service_factory = None

    # Chunk 2:
    # Wiring seams for the job/candidate/matching agents:
    #
    # - JDAnalyzerAgent
    # - ResumeParserAgent
    # - ResumeMatcherAgent
    #
    # None means the agent resolves its own default LLM provider.
    app.state.jd_analyzer_factory = None
    app.state.resume_parser_factory = None
    app.state.resume_matcher_factory = None

    # Chunk 4:
    # Bundled wiring seam for the seven evaluation agents.
    app.state.evaluation_agent_factories = None

    # Chunk 5:
    # Wiring seam for LeaderboardAgent used by RecruiterService.
    app.state.leaderboard_factory = None

    # Make the application settings available through app.state.
    app.state.settings = settings

    # ------------------------------------------------------------------
    # Middleware and exception handling
    # ------------------------------------------------------------------

    install_middleware(app, settings)
    register_exception_handlers(app)

    # ------------------------------------------------------------------
    # API routes
    # ------------------------------------------------------------------

    app.include_router(
        auth_router,
        prefix=settings.api_prefix,
    )

    app.include_router(
        admin_router,
        prefix=settings.api_prefix,
    )

    app.include_router(
        interview_router,
        prefix=settings.api_prefix,
    )

    app.include_router(
        voice_router,
        prefix=settings.api_prefix,
    )

    app.include_router(
        interview_mediator_router,
        prefix=settings.api_prefix,
    )

    app.include_router(
        scoring_router,
        prefix=settings.api_prefix,
    )

    app.include_router(
        reports_router,
        prefix=settings.api_prefix,
    )

    app.include_router(
        jobs_router,
        prefix=settings.api_prefix,
    )

    app.include_router(
        rubrics_router,
        prefix=settings.api_prefix,
    )

    app.include_router(
        candidates_router,
        prefix=settings.api_prefix,
    )

    app.include_router(
        applications_router,
        prefix=settings.api_prefix,
    )

    app.include_router(
        evaluations_router,
        prefix=settings.api_prefix,
    )

    app.include_router(
        bugs_router,
        prefix=settings.api_prefix,
    )

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
        """
        Liveness endpoint.

        This endpoint intentionally performs:

        - no LLM call
        - no provider call
        - no database call
        - no API-key validation

        The response remains exactly:

            {"status": "ok"}

        This keeps the endpoint inexpensive enough for continuous
        load-balancer health checks.
        """

        return HealthResponse(status="ok")

    # ------------------------------------------------------------------
    # Root endpoint
    # ------------------------------------------------------------------

    @app.api_route(
        f"{settings.api_prefix}/",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def root(
        container: ServiceContainer = Depends(get_container),
    ) -> dict:
        """
        Return a credential-free summary of the running service.

        Provider names may be exposed, but credentials, connection
        strings, and filesystem paths must never be returned.

        Persistence information is included so deployments using
        temporary in-memory repositories are clearly identified.
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
    # Static pages
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


# ----------------------------------------------------------------------
# Uvicorn / deployment / test entry point
# ----------------------------------------------------------------------

# Keep this module-level application object because:
#
#     uvicorn api.app:app --reload
#
# and existing tests depend on it.
app = create_app()
