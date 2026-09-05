"""
Dependency injection container.

One object, built once during startup and stored at `app.state.container`,
that owns every long-lived collaborator the request handlers need:
repositories, the runtime session registry, the auth provider, and the
services built on top of them.

Why a container rather than more `app.state.<thing>` attributes
---------------------------------------------------------------
`app.state` already carried three ad-hoc wiring seams (`registry`,
`interviewer_factory`, `voice_service_factory`), each documented separately
as "a pure wiring hook". That works for three and stops working well beyond
it: there is no single place that says what the application is made of, no
ordering guarantee between them, and nothing to shut down at the end. The
container gives all of that one home while keeping those three attributes
working exactly as before (see below).

Backwards compatibility is not incidental here
-----------------------------------------------
`tests/test_api.py` and `tests/test_voice_layer.py` assign
`app.state.registry`, `app.state.interviewer_factory` and
`app.state.voice_service_factory` directly, and those are legitimate seams,
not test hacks. So the container *reads through* to `app.state` for exactly
those three rather than shadowing them: replacing `app.state.registry` still
replaces the registry the services use. The container is additive.

Construction is the only place stubs are named
-----------------------------------------------
`build_default_container()` is the single site that mentions
`repositories.memory` or `repositories.postgres`. Which one gets built is
governed entirely by `AppSettings.database_url`: empty selects the
in-process stubs (every existing deployment and test), set selects the real
PostgreSQL-backed repositories (repositories/postgres/). Nothing else in
the backend - no service, no route, no other part of this file - needs to
know or care which one is in use; both sides satisfy the exact same ABCs
from repositories/interfaces.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional
from api.registry import SessionRegistry
from core.config import AppSettings
from core.logging import get_logger
from core.security import AnonymousAuthProvider, AuthProvider, JWTAuthProvider
from repositories.interfaces import (
    RubricRepository,
    ApplicationRepository,
    BugReportRepository,
    CandidateRepository,
    EvaluationRepository,
    JobRepository,
    SessionRepository,
    TranscriptRepository,
    UserRepository,
)

if TYPE_CHECKING:  # pragma: no cover - import-time only, never at runtime
    # Deferred so this module (imported unconditionally at startup) does
    # not require asyncpg to be installed unless a database is actually
    # configured - see build_default_container below.
    from repositories.postgres import PostgresConnectionPool
from services.auth_service import AuthService
from services.evaluation_dispatcher import EvaluationDispatcher

logger = get_logger("core.container")


@dataclass
class ServiceContainer:
    """The application's composed dependencies.

    A dataclass, not a service locator: it holds already-constructed
    collaborators and does not resolve anything by name or string key. Code
    that needs something declares it through `core/dependencies.py`.
    """

    settings: AppSettings
    session_repository: SessionRepository
    transcript_repository: TranscriptRepository
    # Chunk 2: job/candidate/application persistence, the same
    # temporary-stub-now / database-later contract as the two repositories
    # above.
    job_repository: JobRepository
    candidate_repository: CandidateRepository
    application_repository: ApplicationRepository
    # Versioned hiring rubrics. A job with no approved rubric is not
    # scorable, so this gates matching entirely.
    rubric_repository: RubricRepository
    # Chunk 4: evaluation job persistence, same stub-now/database-later
    # contract. `evaluation_dispatcher` is NOT a repository - it is the
    # container-level singleton `EvaluationService` schedules background
    # execution through (services/evaluation_dispatcher.py); it must be
    # constructed once here and never per-request, or its in-flight task
    # set (what keeps scheduled evaluations from being garbage-collected)
    # would be recreated empty on every request.
    evaluation_repository: EvaluationRepository
    evaluation_dispatcher: EvaluationDispatcher
    # Chunk 5: user repository for authentication
    user_repository: UserRepository
    # OpenBox: platform-wide, user-reported bug tracker (pages/openbox.html).
    bug_report_repository: BugReportRepository
    auth_provider: AuthProvider = field(default_factory=AnonymousAuthProvider)

    # True while any repository above is one of the temporary in-process
    # stubs. Surfaced on /health and warned about at startup - see
    # repositories/memory.py.
    persistence_is_ephemeral: bool = True

    # The shared PostgreSQL connection pool, when `settings.database_url`
    # is set - None while running on the in-memory stubs. Held here (rather
    # than only inside the repositories that use it) purely so `aclose()`
    # can release it at shutdown; nothing else in the backend touches this
    # field directly.
    database_pool: Optional[PostgresConnectionPool] = None

    # The runtime registry of live InterviewSessionRunner objects. Optional
    # because the canonical location remains `app.state.registry` (see the
    # module docstring); this is the fallback used when the container is
    # built without an app, e.g. in a unit test.
    _fallback_registry: SessionRegistry = field(default_factory=SessionRegistry)

    def registry_for(self, app) -> SessionRegistry:
        """The session runtime registry this request should use.

        Reads `app.state.registry` on every call rather than caching it, so
        a test that swaps the registry between requests gets the new one -
        the behaviour tests/test_api.py's `client` fixture depends on.
        """
        registry = getattr(getattr(app, "state", None), "registry", None)
        return registry if registry is not None else self._fallback_registry

    def interviewer_factory_for(self, app) -> Optional[Callable]:
        """The `app.state.interviewer_factory` seam, unchanged. None means
        "use InterviewSessionRunner's own default", exactly as before."""
        return getattr(getattr(app, "state", None), "interviewer_factory", None)

    def voice_service_factory_for(self, app) -> Optional[Callable]:
        """The `app.state.voice_service_factory` seam, unchanged."""
        return getattr(getattr(app, "state", None), "voice_service_factory", None)

    def jd_analyzer_factory_for(self, app) -> Optional[Callable]:
        """Chunk 2: the same kind of pure wiring seam as
        `interviewer_factory_for` above - a zero-arg callable returning a
        `JDAnalyzerAgent`, used in place of `JDAnalyzerAgent()`'s own default
        (which resolves an LLM provider via `get_llm_provider()`). None (the
        default) means "use the agent's normal default"."""
        return getattr(getattr(app, "state", None), "jd_analyzer_factory", None)

    def resume_parser_factory_for(self, app) -> Optional[Callable]:
        """Chunk 2: same pattern, for `ResumeParserAgent`."""
        return getattr(getattr(app, "state", None), "resume_parser_factory", None)

    def resume_matcher_factory_for(self, app) -> Optional[Callable]:
        """Chunk 2: same pattern, for `ResumeMatcherAgent`."""
        return getattr(getattr(app, "state", None), "resume_matcher_factory", None)

    def evaluation_agent_factories_for(self, app):
        """Chunk 4: the same wiring-seam pattern, bundled - see
        `services.evaluation_service.EvaluationAgentFactories`'s docstring
        for why seven agents get one bundled seam instead of seven
        attributes. None means "construct every real agent"."""
        return getattr(getattr(app, "state", None), "evaluation_agent_factories", None)

    def leaderboard_factory_for(self, app) -> Optional[Callable]:
        """Chunk 5: same pattern, for `LeaderboardAgent`
        (`RecruiterService`)."""
        return getattr(getattr(app, "state", None), "leaderboard_factory", None)

    async def aclose(self) -> None:
        """Release anything the container owns, at shutdown.

        Nothing needs closing while the repositories are in-process dicts.
        The hook exists because a database-backed repository will own a
        connection pool, and shutdown is the wrong thing to be retrofitting
        under time pressure at that point. Each close is isolated so one
        failing collaborator cannot prevent the others from being released.
        """
        for name in (
            "session_repository",
            "transcript_repository",
            "job_repository",
            "candidate_repository",
            "application_repository",
            "rubric_repository",
            "evaluation_repository",
            "evaluation_dispatcher",
            "bug_report_repository",
            "auth_provider",
            "database_pool",
        ):
            component = getattr(self, name, None)
            closer = getattr(component, "aclose", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.error(
                    "error closing %s during shutdown: %s", name, exc, exc_info=True
                )


def build_default_container(settings: AppSettings) -> ServiceContainer:
    """Compose the application's dependencies.

    ------------------------------------------------------------------
    THE DATABASE REPLACEMENT POINT.

    Which repositories get built is governed by exactly one thing:
    `settings.database_url`. Empty (the default - no existing deployment or
    test sets it) means the temporary in-process stubs from
    repositories/memory.py, unchanged from before this database chunk
    landed. Set means the real repositories/postgres implementations,
    sharing one lazily-opened connection pool. Nothing else in the backend
    needs to change either way - every caller only ever sees the
    repository ABCs from repositories/interfaces.py.
    ------------------------------------------------------------------
    """
    from services.evaluation_dispatcher import AsyncTaskEvaluationDispatcher

    logger.warning(
        "evaluation background execution is TEMPORARY local asyncio "
        "(services/evaluation_dispatcher.py) - an evaluation still RUNNING "
        "when the process exits is abandoned, not resumed. Replace with a "
        "durable queue/worker in core/container.build_default_container()."
    )

    if settings.database_url:
        from repositories.postgres import (
            POSTGRES_BACKEND_NAME,
            PostgresApplicationRepository,
    PostgresRubricRepository,
            PostgresBugReportRepository,
            PostgresCandidateRepository,
            PostgresConnectionPool,
            PostgresEvaluationRepository,
            PostgresJobRepository,
            PostgresSessionRepository,
            PostgresTranscriptRepository,
            PostgresUserRepository,
        )

        logger.info("persistence backend: %s", POSTGRES_BACKEND_NAME)
        pool = PostgresConnectionPool(settings.database_url)
        user_repo = PostgresUserRepository(pool)

        # Create JWTAuthProvider with AuthService for database-backed deployments
        auth_service = AuthService(user_repository=user_repo, settings=settings)
        auth_provider = JWTAuthProvider(auth_service)

        return ServiceContainer(
            settings=settings,
            session_repository=PostgresSessionRepository(pool),
            transcript_repository=PostgresTranscriptRepository(pool),
            job_repository=PostgresJobRepository(pool),
            candidate_repository=PostgresCandidateRepository(pool),
            application_repository=PostgresApplicationRepository(pool),
            rubric_repository=PostgresRubricRepository(pool),
            evaluation_repository=PostgresEvaluationRepository(pool),
            user_repository=user_repo,
            bug_report_repository=PostgresBugReportRepository(pool),
            evaluation_dispatcher=AsyncTaskEvaluationDispatcher(),
            auth_provider=auth_provider,
            persistence_is_ephemeral=False,
            database_pool=pool,
        )

    from repositories.memory import (
        EPHEMERAL_BACKEND_NAME,
        InMemoryApplicationRepository,
    InMemoryRubricRepository,
        InMemoryBugReportRepository,
        InMemoryCandidateRepository,
        InMemoryEvaluationRepository,
        InMemoryJobRepository,
        InMemorySessionRepository,
        InMemoryTranscriptRepository,
        InMemoryUserRepository,
    )

    logger.warning(
        "persistence is EPHEMERAL: using %s. Interview session records, "
        "sealed transcripts, jobs, candidates, applications and evaluation "
        "jobs will not survive a restart. Set DATABASE_URL to use "
        "repositories/postgres instead.",
        EPHEMERAL_BACKEND_NAME,
    )

    return ServiceContainer(
        settings=settings,
        session_repository=InMemorySessionRepository(),
        transcript_repository=InMemoryTranscriptRepository(),
        job_repository=InMemoryJobRepository(),
        candidate_repository=InMemoryCandidateRepository(),
        application_repository=InMemoryApplicationRepository(),
        rubric_repository=InMemoryRubricRepository(),
        evaluation_repository=InMemoryEvaluationRepository(),
        user_repository=InMemoryUserRepository(),
        bug_report_repository=InMemoryBugReportRepository(),
        evaluation_dispatcher=AsyncTaskEvaluationDispatcher(),
        auth_provider=AnonymousAuthProvider(),
        persistence_is_ephemeral=True,
    )


__all__ = ["ServiceContainer", "build_default_container"]
