"""
Admin console use cases: user management, content moderation, platform
metrics, and support impersonation.

Aggregates the same repositories every other service already depends on
(UserRepository, JobRepository, CandidateRepository, ApplicationRepository)
plus the new AuditLogRepository - no new persistence beyond that, matching
RecruiterService's own "read-only aggregation over existing repositories"
precedent (services/recruiter_service.py).

Every admin action here is only reachable once a caller has already passed
`core.security.require_admin` - this service does not re-check who is
calling; that authorization decision is the route layer's job, exactly like
every other service in this codebase.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Optional

from core.config import AppSettings
from core.errors import BadRequestError, ConflictError, NotFoundError, UnauthorizedError
from core.logging import get_logger, log_context
from repositories.interfaces import (
    AuditLogRecord,
    AuditLogRepository,
    Application,
    ApplicationRepository,
    CandidateRecord,
    CandidateRepository,
    JobRecord,
    JobRepository,
    UserRecord,
    UserRepository,
)
from services.auth_service import AuthService
from services.evaluation_dispatcher import EvaluationDispatcher

if TYPE_CHECKING:  # pragma: no cover - import-time only, never at runtime
    from repositories.postgres import PostgresConnectionPool

logger = get_logger("services.admin")

# The complete set of values PATCH /admin/users/{id} may set user_type to.
# No 'anonymous'/'service' - those are Principal-only concepts, never a
# stored account's user_type (core/security.py:PrincipalType).
_VALID_USER_TYPES = {"candidate", "recruiter", "admin"}

# Fields PATCH /admin/users/{id} may change. Deliberately wider than
# AuthService.update_profile's own allowed set (services/auth_service.py) -
# an admin may change fields a self-service profile update cannot.
_ADMIN_UPDATABLE_USER_FIELDS = {"is_active", "user_type"}


class AdminService:
    """Use cases behind every `/admin/*` route (api/routes/admin.py)."""

    def __init__(
        self,
        *,
        user_repository: UserRepository,
        job_repository: JobRepository,
        candidate_repository: CandidateRepository,
        application_repository: ApplicationRepository,
        audit_log_repository: AuditLogRepository,
        auth_service: AuthService,
        settings: Optional[AppSettings] = None,
        database_pool: Optional["PostgresConnectionPool"] = None,
        evaluation_dispatcher: Optional[EvaluationDispatcher] = None,
    ) -> None:
        # settings/database_pool/evaluation_dispatcher are optional and only
        # used by get_system_status - existing call sites (tests, older
        # wiring) that don't pass them keep working; get_system_status
        # itself requires `settings` and raises without it, see below.
        self._settings = settings
        self._database_pool = database_pool
        self._evaluation_dispatcher = evaluation_dispatcher
        self._users = user_repository
        self._jobs = job_repository
        self._candidates = candidate_repository
        self._applications = application_repository
        self._audit_logs = audit_log_repository
        self._auth = auth_service

    # -- User management -----------------------------------------------

    async def list_users(
        self,
        *,
        query: Optional[str] = None,
        user_type: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> list[UserRecord]:
        return await self._users.list_users(query=query, user_type=user_type, is_active=is_active)

    async def update_user(self, user_id: str, updates: dict) -> UserRecord:
        """Apply a partial update to `is_active`/`user_type`.

        Raises:
            NotFoundError: If no such user exists.
            BadRequestError: If `updates['user_type']` is present and not
                one of candidate/recruiter/admin.
        """
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise NotFoundError(
                "No account found.",
                internal_detail=f"user_id={user_id!r} not found",
            )

        safe_updates = {k: v for k, v in updates.items() if k in _ADMIN_UPDATABLE_USER_FIELDS}

        if "user_type" in safe_updates and safe_updates["user_type"] not in _VALID_USER_TYPES:
            raise BadRequestError(
                f"user_type must be one of {sorted(_VALID_USER_TYPES)}.",
                internal_detail=(
                    f"admin update for user {user_id!r} supplied invalid "
                    f"user_type={safe_updates['user_type']!r}"
                ),
            )

        updated = user.model_copy(update=safe_updates)
        stored = await self._users.save(updated)

        logger.info(
            "admin updated a user account",
            extra=log_context(event="admin_user_updated", user_id=user_id, fields=sorted(safe_updates)),
        )
        return stored

    # -- Moderation: jobs -------------------------------------------------

    async def list_jobs(self, *, include_hidden: bool = False) -> list[JobRecord]:
        return await self._jobs.list_jobs(include_archived=include_hidden)

    async def set_job_active(self, job_id: str, is_active: bool) -> JobRecord:
        """`is_active=False` hides the job (delegates to the existing
        `JobRepository.archive` - indistinguishable from a recruiter's own
        archive action, per spec §3.2/§8); `is_active=True` un-hides it
        (`JobRepository.restore`, Task 6).

        Raises:
            NotFoundError: If no such job exists.
        """
        updated = await self._jobs.archive(job_id) if not is_active else await self._jobs.restore(job_id)
        if updated is None:
            raise NotFoundError(
                "No job found.",
                internal_detail=f"job_id={job_id!r} not found",
            )
        logger.info(
            "admin set job active state",
            extra=log_context(event="admin_job_active_set", job_id=job_id, is_active=is_active),
        )
        return updated

    # -- Moderation: candidates --------------------------------------------

    async def list_candidates(self, *, include_hidden: bool = False) -> list[CandidateRecord]:
        return await self._candidates.list_candidates(include_hidden=include_hidden)

    async def set_candidate_hidden(self, candidate_id: str, is_hidden: bool) -> CandidateRecord:
        """Raises:
            NotFoundError: If no such candidate exists.
        """
        updated = await self._candidates.set_hidden(candidate_id, is_hidden)
        if updated is None:
            raise NotFoundError(
                "No candidate found.",
                internal_detail=f"candidate_id={candidate_id!r} not found",
            )
        logger.info(
            "admin set candidate hidden state",
            extra=log_context(
                event="admin_candidate_hidden_set", candidate_id=candidate_id, is_hidden=is_hidden
            ),
        )
        return updated

    # -- Moderation: applications -------------------------------------------

    async def list_applications(self, *, include_hidden: bool = False) -> list[Application]:
        return await self._applications.list_all(include_hidden=include_hidden)

    async def set_application_hidden(self, application_id: str, is_hidden: bool) -> Application:
        """Raises:
            NotFoundError: If no such application exists.
        """
        updated = await self._applications.set_hidden(application_id, is_hidden)
        if updated is None:
            raise NotFoundError(
                "No application found.",
                internal_detail=f"application_id={application_id!r} not found",
            )
        logger.info(
            "admin set application hidden state",
            extra=log_context(
                event="admin_application_hidden_set",
                application_id=application_id, is_hidden=is_hidden,
            ),
        )
        return updated

    # -- Impersonation --------------------------------------------------

    async def impersonate(self, admin_id: str, target_user_id: str) -> tuple[str, str]:
        """Issue a normal token pair for `target_user_id`, logging exactly
        one `AuditLogRecord` for accountability (spec §5.4).

        Raises:
            NotFoundError: If no such target user exists.
            BadRequestError: If the target account is not active.
        """
        target = await self._users.get_by_id(target_user_id)
        if target is None:
            raise NotFoundError(
                "No account found.",
                internal_detail=f"user_id={target_user_id!r} not found",
            )
        if not target.is_active:
            raise BadRequestError(
                "Cannot impersonate an inactive account.",
                internal_detail=f"user_id={target_user_id!r} is inactive",
            )

        await self._audit_logs.save(AuditLogRecord(
            log_id=f"log_{uuid.uuid4().hex[:12]}",
            admin_id=admin_id,
            action="impersonate",
            target_user_id=target_user_id,
        ))

        access_token, refresh_token = self._auth.issue_tokens_for_user(target)

        logger.info(
            "admin impersonated a user",
            extra=log_context(
                event="admin_impersonate", admin_id=admin_id, target_user_id=target_user_id
            ),
        )
        return access_token, refresh_token

    # -- Metrics ------------------------------------------------------------

    async def get_metrics(self) -> dict:
        """Simple platform counts (spec §5.3), computed from existing
        `list_*` methods - no new aggregation infrastructure. Acceptable per
        spec for phase 1's scale; a future revision can add dedicated
        `count` methods to the repositories if these lists grow large enough
        to make full fetches expensive."""
        users = await self._users.list_users()
        user_counts = {"candidate": 0, "recruiter": 0, "admin": 0}
        for u in users:
            if u.user_type in user_counts:
                user_counts[u.user_type] += 1

        jobs = await self._jobs.list_jobs(include_archived=True)
        active_jobs = sum(1 for j in jobs if j.is_active)
        hidden_jobs = len(jobs) - active_jobs

        applications = await self._applications.list_all(include_hidden=True)
        hidden_applications = sum(1 for a in applications if a.is_hidden)

        return {
            "users": user_counts,
            "jobs": {"active": active_jobs, "hidden": hidden_jobs},
            "applications": {"total": len(applications), "hidden": hidden_applications},
        }

    # -- System status --------------------------------------------------

    _DB_PING_TIMEOUT_SECONDS = 2.0

    async def get_system_status(self) -> dict:
        """Live backend health for the admin dashboard (GET
        /admin/system/status): which persistence/provider config is
        actually active, whether the DB is reachable right now (a live
        `SELECT 1`, not just "a pool object exists"), whether prod is still
        signing tokens with the published placeholder secret, and how many
        evaluations this process currently has in flight.

        Requires `settings` to have been supplied at construction - raises
        RuntimeError otherwise, since that means core/dependencies.py's
        wiring is missing something, not that the caller sent a bad
        request."""
        if self._settings is None:
            raise RuntimeError(
                "AdminService.get_system_status requires `settings` - "
                "construct AdminService with settings=... (see "
                "core/dependencies.py:get_admin_service)."
            )
        settings = self._settings

        database = {"connected": False, "latency_ms": None, "error": None}
        if self._database_pool is None:
            database["error"] = "no DATABASE_URL configured - using in-memory persistence"
        else:
            start = time.perf_counter()
            try:
                pool = await asyncio.wait_for(
                    self._database_pool.get(), timeout=self._DB_PING_TIMEOUT_SECONDS
                )
                await asyncio.wait_for(
                    pool.fetchval("SELECT 1"), timeout=self._DB_PING_TIMEOUT_SECONDS
                )
                database["connected"] = True
                database["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)
            except Exception as exc:  # live health probe - never let this 500 the dashboard
                database["error"] = str(exc)

        return {
            "environment": settings.environment,
            "persistence": "postgres" if self._database_pool is not None else "memory",
            "auth_enabled": settings.auth_enabled,
            "jwt_secret_is_placeholder": settings.jwt_secret_is_default,
            "providers": {
                "llm": settings.llm_provider,
                "embedding": settings.embedding_provider,
                "audio": settings.audio_provider,
                "tts": settings.tts_provider,
            },
            "database": database,
            "evaluation_queue": {
                "running": (
                    self._evaluation_dispatcher.running_count
                    if self._evaluation_dispatcher is not None
                    else 0
                ),
            },
        }


__all__ = ["AdminService"]
