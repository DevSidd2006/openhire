"""
FastAPI dependency providers.

The single place route handlers get their collaborators from. Everything
here is a thin resolver over `app.state.container` (core/container.py) - no
construction logic beyond assembling a per-request service object, and no
business rules.

Why route handlers should not read `request.app.state` themselves: they did,
and it meant the set of things a handler depended on was invisible in its
signature and impossible to override for one test without mutating global
app state. Declaring dependencies makes them appear in the signature, in the
OpenAPI schema, and in FastAPI's `dependency_overrides` map.
"""
from __future__ import annotations

from fastapi import Request, WebSocket

from api.registry import SessionRegistry
from core.config import AppSettings, get_settings
from core.container import ServiceContainer, build_default_container
from repositories.interfaces import (
    RubricRepository,
    ApplicationRepository,
    AuditLogRepository,
    CandidateRepository,
    EvaluationRepository,
    JobRepository,
    SessionRepository,
    TranscriptRepository,
    UserRepository,
)
from services.admin_service import AdminService
from services.application_service import ApplicationService
from services.auth_service import AuthService
from services.bug_report_service import BugReportService
from services.candidate_service import CandidateService
from services.evaluation_service import EvaluationService
from services.interview_service import InterviewService
from services.job_service import JobService
from services.matching_service import MatchingService
from services.recruiter_service import RecruiterService


def _container_from_app(app) -> ServiceContainer:
    """Resolve the container, building a default one if the app was created
    outside the normal lifespan.

    The fallback matters for unit tests that instantiate an app directly. It
    is cached back onto `app.state` so repeated requests share one container
    rather than silently getting a fresh set of (empty) repositories each
    time - which would look like data randomly disappearing.
    """
    container = getattr(app.state, "container", None)
    if container is None:
        container = build_default_container(get_settings())
        app.state.container = container
    return container


def get_container(request: Request) -> ServiceContainer:
    return _container_from_app(request.app)


def get_app_settings(request: Request) -> AppSettings:
    """Settings as a dependency, taken from the container so a test that
    injects a differently-configured container is honoured."""
    return _container_from_app(request.app).settings


def get_registry(request: Request) -> SessionRegistry:
    """The runtime registry of live sessions.

    Resolved through the container, which reads `app.state.registry` on
    every call - preserving the existing seam that tests replace between
    requests.
    """
    return _container_from_app(request.app).registry_for(request.app)


def get_session_repository(request: Request) -> SessionRepository:
    return _container_from_app(request.app).session_repository


def get_transcript_repository(request: Request) -> TranscriptRepository:
    return _container_from_app(request.app).transcript_repository


def get_job_repository(request: Request) -> JobRepository:
    return _container_from_app(request.app).job_repository


def get_candidate_repository(request: Request) -> CandidateRepository:
    return _container_from_app(request.app).candidate_repository


def get_application_repository(request: Request) -> ApplicationRepository:
    return _container_from_app(request.app).application_repository


def get_rubric_repository(request: Request) -> RubricRepository:
    return get_container(request).rubric_repository


def get_evaluation_repository(request: Request) -> EvaluationRepository:
    return _container_from_app(request.app).evaluation_repository


def get_user_repository(request: Request) -> UserRepository:
    return _container_from_app(request.app).user_repository


def get_audit_log_repository(request: Request) -> AuditLogRepository:
    return _container_from_app(request.app).audit_log_repository


def get_auth_service(request: Request) -> AuthService:
    """Assemble an `AuthService` for one request."""
    container = _container_from_app(request.app)
    return AuthService(
        user_repository=container.user_repository,
        settings=container.settings,
    )


def build_interview_service(app) -> InterviewService:
    """Assemble an `InterviewService` for one request.

    Kept as a plain function taking `app` (not a FastAPI dependency) so the
    WebSocket handler, which has a `WebSocket` rather than a `Request`, can
    use the identical construction path. Two divergent ways to build the
    service is exactly how the REST and WebSocket transports would drift
    apart.
    """
    container = _container_from_app(app)
    return InterviewService(
        registry=container.registry_for(app),
        session_repository=container.session_repository,
        transcript_repository=container.transcript_repository,
        interviewer_factory=container.interviewer_factory_for(app),
    )


def get_interview_service(request: Request) -> InterviewService:
    return build_interview_service(request.app)


def get_interview_service_ws(websocket: WebSocket) -> InterviewService:
    """The WebSocket-side equivalent of `get_interview_service`."""
    return build_interview_service(websocket.app)


def get_job_service(request: Request) -> JobService:
    container = _container_from_app(request.app)
    return JobService(
        job_repository=container.job_repository,
        jd_analyzer_factory=container.jd_analyzer_factory_for(request.app),
    )


def get_candidate_service(request: Request) -> CandidateService:
    container = _container_from_app(request.app)
    return CandidateService(
        candidate_repository=container.candidate_repository,
        resume_parser_factory=container.resume_parser_factory_for(request.app),
    )


def get_bug_report_service(request: Request) -> BugReportService:
    container = _container_from_app(request.app)
    return BugReportService(bug_report_repository=container.bug_report_repository)


def get_matching_service(request: Request) -> MatchingService:
    container = _container_from_app(request.app)
    return MatchingService(
        resume_matcher_factory=container.resume_matcher_factory_for(request.app),
    )


def get_application_service(request: Request) -> ApplicationService:
    container = _container_from_app(request.app)
    return ApplicationService(
        application_repository=container.application_repository,
        job_repository=container.job_repository,
        candidate_repository=container.candidate_repository,
        matching_service=get_matching_service(request),
        rubric_repository=container.rubric_repository,
    )


def build_evaluation_service(app) -> EvaluationService:
    """Assemble an `EvaluationService` for one request.

    A plain function taking `app` (not a FastAPI dependency), same reason as
    `build_interview_service`: the voice WebSocket handler
    (api/routes/voice.py) triggers evaluation after a voice turn seals a
    session too, and has a `WebSocket`, not a `Request`.

    `dispatcher=container.evaluation_dispatcher` is the one dependency here
    that is NOT re-resolved through an `app.state.*` seam the way registry/
    interviewer_factory are - the dispatcher is a container-level singleton
    with no equivalent "swap it mid-test" seam, because unlike a registry
    swap (simulating a restart), replacing the dispatcher mid-session has no
    real-world analog worth supporting.
    """
    container = _container_from_app(app)
    return EvaluationService(
        evaluation_repository=container.evaluation_repository,
        session_repository=container.session_repository,
        transcript_repository=container.transcript_repository,
        application_repository=container.application_repository,
        dispatcher=container.evaluation_dispatcher,
        agent_factories=container.evaluation_agent_factories_for(app),
    )


def get_evaluation_service(request: Request) -> EvaluationService:
    return build_evaluation_service(request.app)


def get_evaluation_service_ws(websocket: WebSocket) -> EvaluationService:
    """The WebSocket-side equivalent of `get_evaluation_service`."""
    return build_evaluation_service(websocket.app)


def get_recruiter_service(request: Request) -> RecruiterService:
    """Chunk 5: `RecruiterService` is read-only aggregation over the SAME
    repositories every other service already depends on - no new
    persistence, so unlike `EvaluationService` there is no dispatcher or
    long-lived singleton to worry about getting wrong here."""
    container = _container_from_app(request.app)
    return RecruiterService(
        job_repository=container.job_repository,
        application_repository=container.application_repository,
        candidate_repository=container.candidate_repository,
        session_repository=container.session_repository,
        evaluation_repository=container.evaluation_repository,
        leaderboard_factory=container.leaderboard_factory_for(request.app),
    )


def get_admin_service(request: Request) -> AdminService:
    container = _container_from_app(request.app)
    return AdminService(
        user_repository=container.user_repository,
        job_repository=container.job_repository,
        candidate_repository=container.candidate_repository,
        application_repository=container.application_repository,
        audit_log_repository=container.audit_log_repository,
        auth_service=AuthService(
            user_repository=container.user_repository, settings=container.settings
        ),
        settings=container.settings,
        database_pool=container.database_pool,
        evaluation_dispatcher=container.evaluation_dispatcher,
    )


__all__ = [
    "build_evaluation_service",
    "build_interview_service",
    "get_admin_service",
    "get_app_settings",
    "get_application_repository",
    "get_application_service",
    "get_audit_log_repository",
    "get_auth_service",
    "get_bug_report_service",
    "get_candidate_repository",
    "get_candidate_service",
    "get_container",
    "get_evaluation_repository",
    "get_evaluation_service",
    "get_evaluation_service_ws",
    "get_interview_service",
    "get_interview_service_ws",
    "get_job_repository",
    "get_job_service",
    "get_matching_service",
    "get_recruiter_service",
    "get_registry",
    "get_session_repository",
    "get_transcript_repository",
    "get_user_repository",
]
