"""
Admin console backend: user_type == 'admin', require_admin, the audit log,
moderation flags, and the /admin router (user management, moderation,
metrics, impersonation).

No self-service admin signup exists anywhere - see
schemas/auth.py:SignupRequest's user_type pattern, unchanged by this module.
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from core.security import PrincipalType
from services.auth_service import AuthService
from repositories.memory import InMemoryUserRepository
from core.config import AppSettings


class TestAdminPrincipalType:
    def test_verify_access_token_maps_admin_user_type_to_admin_principal(self):
        settings = AppSettings()
        service = AuthService(user_repository=InMemoryUserRepository(), settings=settings)
        assert service._map_user_type_to_principal_type("admin") is PrincipalType.ADMIN

    def test_admin_gets_admin_scopes(self):
        settings = AppSettings()
        service = AuthService(user_repository=InMemoryUserRepository(), settings=settings)
        scopes = service._get_scopes_for_user_type("admin")
        assert "admin:read" in scopes
        assert "admin:write" in scopes


# ---------------------------------------------------------------------------
# AuditLogRecord / AuditLogRepository
# ---------------------------------------------------------------------------

from repositories.interfaces import AuditLogRecord
from repositories.memory import InMemoryAuditLogRepository


class TestAuditLogRepository:
    @pytest.mark.asyncio
    async def test_save_and_list_for_admin(self):
        repo = InMemoryAuditLogRepository()
        record = AuditLogRecord(
            log_id="log_1", admin_id="user_admin", action="impersonate",
            target_user_id="user_target",
        )
        saved = await repo.save(record)
        assert saved.log_id == "log_1"

        rows = await repo.list_for_admin("user_admin")
        assert len(rows) == 1
        assert rows[0].target_user_id == "user_target"

        assert await repo.list_for_admin("user_someone_else") == []


# ---------------------------------------------------------------------------
# CandidateRecord.is_hidden / CandidateRepository moderation
# ---------------------------------------------------------------------------

from repositories.interfaces import CandidateRecord
from repositories.memory import InMemoryCandidateRepository
from schemas.resume import ParsedResume


def _bare_resume(candidate_id: str) -> ParsedResume:
    return ParsedResume(candidate_id=candidate_id, candidate_name="Jane Doe")


class TestCandidateModeration:
    def test_is_hidden_defaults_to_false(self):
        record = CandidateRecord(
            candidate_id="cand_1", user_id="user_1", resume=_bare_resume("cand_1"),
        )
        assert record.is_hidden is False

    @pytest.mark.asyncio
    async def test_list_candidates_excludes_hidden_by_default(self):
        repo = InMemoryCandidateRepository()
        await repo.save(CandidateRecord(
            candidate_id="cand_1", user_id="user_1", resume=_bare_resume("cand_1"),
        ))
        await repo.save(CandidateRecord(
            candidate_id="cand_2", user_id="user_2", resume=_bare_resume("cand_2"),
            is_hidden=True,
        ))
        visible = await repo.list_candidates()
        assert [c.candidate_id for c in visible] == ["cand_1"]

        everyone = await repo.list_candidates(include_hidden=True)
        assert {c.candidate_id for c in everyone} == {"cand_1", "cand_2"}

    @pytest.mark.asyncio
    async def test_set_hidden_toggles_and_is_reversible(self):
        repo = InMemoryCandidateRepository()
        await repo.save(CandidateRecord(
            candidate_id="cand_1", user_id="user_1", resume=_bare_resume("cand_1"),
        ))
        hidden = await repo.set_hidden("cand_1", True)
        assert hidden.is_hidden is True
        restored = await repo.set_hidden("cand_1", False)
        assert restored.is_hidden is False

    @pytest.mark.asyncio
    async def test_set_hidden_unknown_candidate_returns_none(self):
        repo = InMemoryCandidateRepository()
        assert await repo.set_hidden("does_not_exist", True) is None


# ---------------------------------------------------------------------------
# Application.is_hidden / ApplicationRepository moderation
# ---------------------------------------------------------------------------

from schemas.application import Application
from repositories.memory import InMemoryApplicationRepository


class TestApplicationModeration:
    def test_is_hidden_defaults_to_false(self):
        app = Application(application_id="app_1", job_id="job_1", candidate_id="cand_1")
        assert app.is_hidden is False

    @pytest.mark.asyncio
    async def test_list_all_excludes_hidden_by_default(self):
        repo = InMemoryApplicationRepository()
        await repo.save(Application(application_id="app_1", job_id="job_1", candidate_id="cand_1"))
        await repo.save(Application(
            application_id="app_2", job_id="job_1", candidate_id="cand_2", is_hidden=True,
        ))
        visible = await repo.list_all()
        assert [a.application_id for a in visible] == ["app_1"]
        everyone = await repo.list_all(include_hidden=True)
        assert {a.application_id for a in everyone} == {"app_1", "app_2"}

    @pytest.mark.asyncio
    async def test_list_for_job_excludes_hidden_by_default(self):
        repo = InMemoryApplicationRepository()
        await repo.save(Application(application_id="app_1", job_id="job_1", candidate_id="cand_1"))
        await repo.save(Application(
            application_id="app_2", job_id="job_1", candidate_id="cand_2", is_hidden=True,
        ))
        visible = await repo.list_for_job("job_1")
        assert [a.application_id for a in visible] == ["app_1"]
        everyone = await repo.list_for_job("job_1", include_hidden=True)
        assert len(everyone) == 2

    @pytest.mark.asyncio
    async def test_set_hidden_toggles_and_is_reversible(self):
        repo = InMemoryApplicationRepository()
        await repo.save(Application(application_id="app_1", job_id="job_1", candidate_id="cand_1"))
        hidden = await repo.set_hidden("app_1", True)
        assert hidden.is_hidden is True
        restored = await repo.set_hidden("app_1", False)
        assert restored.is_hidden is False

    @pytest.mark.asyncio
    async def test_set_hidden_unknown_application_returns_none(self):
        repo = InMemoryApplicationRepository()
        assert await repo.set_hidden("does_not_exist", True) is None


# ---------------------------------------------------------------------------
# JobRepository.restore
# ---------------------------------------------------------------------------

from repositories.memory import InMemoryJobRepository
from repositories.interfaces import JobRecord
from schemas.job import JobDescription


def _bare_job(job_id: str) -> JobDescription:
    return JobDescription(job_id=job_id, title="Engineer", description="Build things.")


class TestJobRestore:
    @pytest.mark.asyncio
    async def test_restore_reactivates_an_archived_job(self):
        repo = InMemoryJobRepository()
        await repo.save(JobRecord(job_id="job_1", job=_bare_job("job_1")))
        await repo.archive("job_1")
        restored = await repo.restore("job_1")
        assert restored.is_active is True

    @pytest.mark.asyncio
    async def test_restore_unknown_job_returns_none(self):
        repo = InMemoryJobRepository()
        assert await repo.restore("does_not_exist") is None

    @pytest.mark.asyncio
    async def test_restore_is_idempotent(self):
        repo = InMemoryJobRepository()
        await repo.save(JobRecord(job_id="job_1", job=_bare_job("job_1")))
        first = await repo.restore("job_1")
        second = await repo.restore("job_1")
        assert first.is_active is True
        assert second.is_active is True


# ---------------------------------------------------------------------------
# require_admin
# ---------------------------------------------------------------------------

from fastapi import Depends, FastAPI
from core.security import require_admin, JWTAuthProvider
from core.container import ServiceContainer
from repositories.memory import (
    InMemoryApplicationRepository,
    InMemoryAuditLogRepository,
    InMemoryBugReportRepository,
    InMemoryCandidateRepository,
    InMemoryEvaluationRepository,
    InMemoryJobRepository,
    InMemoryRubricRepository,
    InMemorySessionRepository,
    InMemoryTranscriptRepository,
    InMemoryUserRepository,
)
from services.evaluation_dispatcher import AsyncTaskEvaluationDispatcher
from repositories.interfaces import UserRecord
from core.config import get_settings
from api.errors import register_exception_handlers


def _admin_app():
    """A minimal FastAPI app with one require_admin-gated route, wired the
    same way _authenticated_app() is in tests/test_backend_profile.py."""
    settings = AppSettings(auth_enabled=True)
    user_repo = InMemoryUserRepository()
    auth_service = AuthService(user_repository=user_repo, settings=settings)
    container = ServiceContainer(
        settings=settings,
        session_repository=InMemorySessionRepository(),
        transcript_repository=InMemoryTranscriptRepository(),
        job_repository=InMemoryJobRepository(),
        candidate_repository=InMemoryCandidateRepository(),
        application_repository=InMemoryApplicationRepository(),
        rubric_repository=InMemoryRubricRepository(),
        evaluation_repository=InMemoryEvaluationRepository(),
        user_repository=user_repo,
        bug_report_repository=InMemoryBugReportRepository(),
        audit_log_repository=InMemoryAuditLogRepository(),
        evaluation_dispatcher=AsyncTaskEvaluationDispatcher(),
        auth_provider=JWTAuthProvider(auth_service),
        persistence_is_ephemeral=True,
    )
    app = FastAPI()
    app.state.container = container
    register_exception_handlers(app)

    @app.get("/admin-only")
    async def admin_only(principal=Depends(require_admin)):
        return {"subject_id": principal.subject_id}

    # core/security.py's dependencies read AppSettings via the process-wide
    # core.config.get_settings() (an lru_cache singleton), not through the
    # container - so without this override every request here would see
    # whatever auth_enabled value happened to be cached first (false, per
    # tests/conftest.py), making require_admin inert regardless of this
    # app's own settings. Mirrors _authenticated_app() in
    # tests/test_backend_profile.py.
    app.dependency_overrides[get_settings] = lambda: settings
    return app, auth_service, user_repo


class TestRequireAdmin:
    def test_rejects_unauthenticated_caller(self):
        app, _auth_service, _user_repo = _admin_app()
        client = TestClient(app)
        response = client.get("/admin-only")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_rejects_a_non_admin_caller(self):
        app, auth_service, _user_repo = _admin_app()
        client = TestClient(app)
        _user, access_token, _refresh = await auth_service.signup(
            email="candidate@example.com", password="password123", user_type="candidate",
        )
        response = client.get("/admin-only", headers={"Authorization": f"Bearer {access_token}"})
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_allows_an_admin_caller(self):
        app, auth_service, user_repo = _admin_app()
        client = TestClient(app)
        user, access_token, _refresh = await auth_service.signup(
            email="recruiter@example.com", password="password123", user_type="recruiter",
        )
        # Promote out-of-band, mirroring scripts/promote_admin.py (Task 8).
        await user_repo.save(user.model_copy(update={"user_type": "admin"}))
        response = client.get("/admin-only", headers={"Authorization": f"Bearer {access_token}"})
        assert response.status_code == 200
        assert response.json()["subject_id"] == user.user_id

    @pytest.mark.asyncio
    async def test_a_demoted_admin_loses_access_immediately(self):
        """The JWT still claims user_type=admin (tokens are not re-issued on
        a role change) - require_admin must re-check the CURRENT UserRecord,
        not the token's stale claim."""
        app, auth_service, user_repo = _admin_app()
        client = TestClient(app)
        user, _access, _refresh = await auth_service.signup(
            email="admin@example.com", password="password123", user_type="recruiter",
        )
        await user_repo.save(user.model_copy(update={"user_type": "admin"}))
        _admin_user, admin_token, _r = await auth_service.login(user.email, "password123")

        response = client.get("/admin-only", headers={"Authorization": f"Bearer {admin_token}"})
        assert response.status_code == 200

        await user_repo.save(user.model_copy(update={"user_type": "recruiter"}))
        response_after_demotion = client.get(
            "/admin-only", headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response_after_demotion.status_code == 403


# ---------------------------------------------------------------------------
# UserRepository.list_users
# ---------------------------------------------------------------------------

class TestListUsers:
    @pytest.mark.asyncio
    async def test_lists_every_user_with_no_filters(self):
        repo = InMemoryUserRepository()
        await repo.save(UserRecord(
            user_id="u1", email="a@example.com", password_hash="h", user_type="candidate",
        ))
        await repo.save(UserRecord(
            user_id="u2", email="b@example.com", password_hash="h", user_type="recruiter",
        ))
        users = await repo.list_users()
        assert {u.user_id for u in users} == {"u1", "u2"}

    @pytest.mark.asyncio
    async def test_filters_by_user_type(self):
        repo = InMemoryUserRepository()
        await repo.save(UserRecord(
            user_id="u1", email="a@example.com", password_hash="h", user_type="candidate",
        ))
        await repo.save(UserRecord(
            user_id="u2", email="b@example.com", password_hash="h", user_type="recruiter",
        ))
        users = await repo.list_users(user_type="recruiter")
        assert [u.user_id for u in users] == ["u2"]

    @pytest.mark.asyncio
    async def test_filters_by_is_active(self):
        repo = InMemoryUserRepository()
        await repo.save(UserRecord(
            user_id="u1", email="a@example.com", password_hash="h", user_type="candidate",
            is_active=False,
        ))
        await repo.save(UserRecord(
            user_id="u2", email="b@example.com", password_hash="h", user_type="candidate",
        ))
        inactive = await repo.list_users(is_active=False)
        assert [u.user_id for u in inactive] == ["u1"]

    @pytest.mark.asyncio
    async def test_filters_by_query_matching_email_case_insensitively(self):
        repo = InMemoryUserRepository()
        await repo.save(UserRecord(
            user_id="u1", email="jane.doe@example.com", password_hash="h", user_type="candidate",
        ))
        await repo.save(UserRecord(
            user_id="u2", email="bob@example.com", password_hash="h", user_type="candidate",
        ))
        matches = await repo.list_users(query="JANE")
        assert [u.user_id for u in matches] == ["u1"]


# ---------------------------------------------------------------------------
# schemas/admin.py + AdminService: user management
# ---------------------------------------------------------------------------

from schemas.admin import AdminUpdateUserRequest, AdminUserListResponse
from services.admin_service import AdminService
from repositories.memory import InMemoryJobRepository as _Jobs
from repositories.memory import InMemoryCandidateRepository as _Candidates
from repositories.memory import InMemoryApplicationRepository as _Applications
from core.errors import BadRequestError, NotFoundError


def _admin_service(user_repo=None, auth_service=None):
    settings = AppSettings()
    user_repo = user_repo or InMemoryUserRepository()
    auth_service = auth_service or AuthService(user_repository=user_repo, settings=settings)
    return AdminService(
        user_repository=user_repo,
        job_repository=_Jobs(),
        candidate_repository=_Candidates(),
        application_repository=_Applications(),
        audit_log_repository=InMemoryAuditLogRepository(),
        auth_service=auth_service,
    )


class TestAdminUserListResponse:
    def test_from_records_excludes_password_hash(self):
        record = UserRecord(
            user_id="u1", email="a@example.com", password_hash="secret", user_type="candidate",
        )
        response = AdminUserListResponse.from_records([record])
        assert response.total == 1
        dumped = response.model_dump_json()
        assert "secret" not in dumped


class TestAdminServiceListUsers:
    @pytest.mark.asyncio
    async def test_delegates_to_the_repository_with_all_filters(self):
        user_repo = InMemoryUserRepository()
        await user_repo.save(UserRecord(
            user_id="u1", email="a@example.com", password_hash="h", user_type="recruiter",
        ))
        service = _admin_service(user_repo=user_repo)
        users = await service.list_users(user_type="recruiter")
        assert [u.user_id for u in users] == ["u1"]


class TestAdminServiceUpdateUser:
    @pytest.mark.asyncio
    async def test_updates_is_active(self):
        user_repo = InMemoryUserRepository()
        auth_service = AuthService(user_repository=user_repo, settings=AppSettings())
        user, _a, _r = await auth_service.signup(
            email="c@example.com", password="password123", user_type="candidate",
        )
        service = _admin_service(user_repo=user_repo, auth_service=auth_service)
        updated = await service.update_user(user.user_id, {"is_active": False})
        assert updated.is_active is False

    @pytest.mark.asyncio
    async def test_promotes_a_user_to_admin(self):
        user_repo = InMemoryUserRepository()
        auth_service = AuthService(user_repository=user_repo, settings=AppSettings())
        user, _a, _r = await auth_service.signup(
            email="r@example.com", password="password123", user_type="recruiter",
        )
        service = _admin_service(user_repo=user_repo, auth_service=auth_service)
        updated = await service.update_user(user.user_id, {"user_type": "admin"})
        assert updated.user_type == "admin"

    @pytest.mark.asyncio
    async def test_rejects_an_invalid_user_type(self):
        user_repo = InMemoryUserRepository()
        auth_service = AuthService(user_repository=user_repo, settings=AppSettings())
        user, _a, _r = await auth_service.signup(
            email="c@example.com", password="password123", user_type="candidate",
        )
        service = _admin_service(user_repo=user_repo, auth_service=auth_service)
        with pytest.raises(BadRequestError):
            await service.update_user(user.user_id, {"user_type": "superuser"})

    @pytest.mark.asyncio
    async def test_unknown_user_is_not_found(self):
        service = _admin_service()
        with pytest.raises(NotFoundError):
            await service.update_user("does_not_exist", {"is_active": False})


# ---------------------------------------------------------------------------
# HTTP layer: GET/PATCH /admin/users
# ---------------------------------------------------------------------------

from api.app import create_app


def _authenticated_admin_app():
    """Same shape as tests/test_backend_profile.py's _authenticated_app(),
    reused here (and by every later admin HTTP test) because every /admin
    route needs the identical real-JWTAuthProvider-over-in-memory wiring."""
    settings = AppSettings(auth_enabled=True)
    user_repo = InMemoryUserRepository()
    auth_service = AuthService(user_repository=user_repo, settings=settings)
    container = ServiceContainer(
        settings=settings,
        session_repository=InMemorySessionRepository(),
        transcript_repository=InMemoryTranscriptRepository(),
        job_repository=InMemoryJobRepository(),
        candidate_repository=InMemoryCandidateRepository(),
        application_repository=InMemoryApplicationRepository(),
        rubric_repository=InMemoryRubricRepository(),
        evaluation_repository=InMemoryEvaluationRepository(),
        user_repository=user_repo,
        bug_report_repository=InMemoryBugReportRepository(),
        audit_log_repository=InMemoryAuditLogRepository(),
        evaluation_dispatcher=AsyncTaskEvaluationDispatcher(),
        auth_provider=JWTAuthProvider(auth_service),
        persistence_is_ephemeral=True,
    )
    app = create_app(settings)
    app.state.container = container
    # See tests/test_backend_profile.py's _authenticated_app(): core/security.py
    # reads AppSettings via the process-wide core.config.get_settings() (an
    # lru_cache singleton), not through the container, so without this
    # override require_admin would see whatever auth_enabled value happened
    # to be cached first rather than this app's own settings.
    app.dependency_overrides[get_settings] = lambda: settings
    return app, auth_service, user_repo


async def _signup_admin(auth_service, user_repo, email="admin@example.com"):
    user, _access, _refresh = await auth_service.signup(
        email=email, password="password123", user_type="recruiter",
    )
    await user_repo.save(user.model_copy(update={"user_type": "admin"}))
    _promoted, access_token, _refresh2 = await auth_service.login(email, "password123")
    return _promoted, access_token


class TestAdminUsersEndpoint:
    def test_requires_admin(self):
        app, auth_service, user_repo = _authenticated_admin_app()
        client = TestClient(app)
        signup = client.post(
            "/auth/signup",
            json={"email": "c@example.com", "password": "password123", "user_type": "candidate"},
        )
        token = signup.json()["access_token"]
        response = client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403

    def test_lists_users_for_an_admin(self):
        app, auth_service, user_repo = _authenticated_admin_app()
        client = TestClient(app)
        client.post(
            "/auth/signup",
            json={"email": "c@example.com", "password": "password123", "user_type": "candidate"},
        )

        import asyncio
        _admin_user, admin_token = asyncio.run(
            _signup_admin(auth_service, user_repo)
        )

        response = client.get("/admin/users", headers={"Authorization": f"Bearer {admin_token}"})
        assert response.status_code == 200
        body = response.json()
        emails = {u["email"] for u in body["users"]}
        assert "c@example.com" in emails
        assert "admin@example.com" in emails
        assert all("password_hash" not in u for u in body["users"])

    def test_filters_by_user_type_query_param(self):
        app, auth_service, user_repo = _authenticated_admin_app()
        client = TestClient(app)
        client.post(
            "/auth/signup",
            json={"email": "c@example.com", "password": "password123", "user_type": "candidate"},
        )
        import asyncio
        _admin_user, admin_token = asyncio.run(
            _signup_admin(auth_service, user_repo)
        )
        response = client.get(
            "/admin/users?user_type=candidate", headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        body = response.json()
        assert all(u["user_type"] == "candidate" for u in body["users"])

    def test_patch_promotes_a_user_to_admin(self):
        app, auth_service, user_repo = _authenticated_admin_app()
        client = TestClient(app)
        signup = client.post(
            "/auth/signup",
            json={"email": "r@example.com", "password": "password123", "user_type": "recruiter"},
        )
        target_user_id = signup.json()["user"]["user_id"]
        import asyncio
        _admin_user, admin_token = asyncio.run(
            _signup_admin(auth_service, user_repo)
        )
        response = client.patch(
            f"/admin/users/{target_user_id}",
            json={"user_type": "admin"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert response.status_code == 200
        assert response.json()["user_type"] == "admin"

    def test_patch_rejects_invalid_user_type(self):
        app, auth_service, user_repo = _authenticated_admin_app()
        client = TestClient(app)
        signup = client.post(
            "/auth/signup",
            json={"email": "r@example.com", "password": "password123", "user_type": "recruiter"},
        )
        target_user_id = signup.json()["user"]["user_id"]
        import asyncio
        _admin_user, admin_token = asyncio.run(
            _signup_admin(auth_service, user_repo)
        )
        response = client.patch(
            f"/admin/users/{target_user_id}",
            json={"user_type": "superuser"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert response.status_code == 400

    def test_requires_authentication(self):
        app, _auth_service, _user_repo = _authenticated_admin_app()
        client = TestClient(app)
        response = client.get("/admin/users")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# AdminService: job moderation
# ---------------------------------------------------------------------------

class TestAdminServiceJobModeration:
    @pytest.mark.asyncio
    async def test_list_jobs_excludes_archived_by_default(self):
        jobs = _Jobs()
        await jobs.save(JobRecord(job_id="job_1", job=_bare_job("job_1")))
        await jobs.archive("job_1")
        service = AdminService(
            user_repository=InMemoryUserRepository(), job_repository=jobs,
            candidate_repository=_Candidates(), application_repository=_Applications(),
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=InMemoryUserRepository(), settings=AppSettings()),
        )
        assert await service.list_jobs() == []
        assert len(await service.list_jobs(include_hidden=True)) == 1

    @pytest.mark.asyncio
    async def test_set_job_active_false_archives(self):
        jobs = _Jobs()
        await jobs.save(JobRecord(job_id="job_1", job=_bare_job("job_1")))
        service = AdminService(
            user_repository=InMemoryUserRepository(), job_repository=jobs,
            candidate_repository=_Candidates(), application_repository=_Applications(),
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=InMemoryUserRepository(), settings=AppSettings()),
        )
        updated = await service.set_job_active("job_1", False)
        assert updated.is_active is False

    @pytest.mark.asyncio
    async def test_set_job_active_true_restores(self):
        jobs = _Jobs()
        await jobs.save(JobRecord(job_id="job_1", job=_bare_job("job_1")))
        await jobs.archive("job_1")
        service = AdminService(
            user_repository=InMemoryUserRepository(), job_repository=jobs,
            candidate_repository=_Candidates(), application_repository=_Applications(),
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=InMemoryUserRepository(), settings=AppSettings()),
        )
        updated = await service.set_job_active("job_1", True)
        assert updated.is_active is True

    @pytest.mark.asyncio
    async def test_set_job_active_unknown_job_is_not_found(self):
        service = AdminService(
            user_repository=InMemoryUserRepository(), job_repository=_Jobs(),
            candidate_repository=_Candidates(), application_repository=_Applications(),
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=InMemoryUserRepository(), settings=AppSettings()),
        )
        with pytest.raises(NotFoundError):
            await service.set_job_active("does_not_exist", False)


# ---------------------------------------------------------------------------
# AdminService: candidate moderation
# ---------------------------------------------------------------------------

def _admin_service_full():
    settings = AppSettings()
    user_repo = InMemoryUserRepository()
    return AdminService(
        user_repository=user_repo, job_repository=_Jobs(),
        candidate_repository=_Candidates(), application_repository=_Applications(),
        audit_log_repository=InMemoryAuditLogRepository(),
        auth_service=AuthService(user_repository=user_repo, settings=settings),
    )


class TestAdminServiceCandidateModeration:
    @pytest.mark.asyncio
    async def test_set_candidate_hidden_true_hides(self):
        candidates = _Candidates()
        await candidates.save(CandidateRecord(
            candidate_id="cand_1", user_id="user_1", resume=_bare_resume("cand_1"),
        ))
        service = AdminService(
            user_repository=InMemoryUserRepository(), job_repository=_Jobs(),
            candidate_repository=candidates, application_repository=_Applications(),
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=InMemoryUserRepository(), settings=AppSettings()),
        )
        updated = await service.set_candidate_hidden("cand_1", True)
        assert updated.is_hidden is True
        restored = await service.set_candidate_hidden("cand_1", False)
        assert restored.is_hidden is False

    @pytest.mark.asyncio
    async def test_set_candidate_hidden_unknown_is_not_found(self):
        service = _admin_service_full()
        with pytest.raises(NotFoundError):
            await service.set_candidate_hidden("does_not_exist", True)


# ---------------------------------------------------------------------------
# AdminService: application moderation
# ---------------------------------------------------------------------------

class TestAdminServiceApplicationModeration:
    @pytest.mark.asyncio
    async def test_list_applications_excludes_hidden_by_default(self):
        applications = _Applications()
        await applications.save(Application(application_id="app_1", job_id="job_1", candidate_id="cand_1"))
        await applications.save(Application(
            application_id="app_2", job_id="job_1", candidate_id="cand_2", is_hidden=True,
        ))
        service = AdminService(
            user_repository=InMemoryUserRepository(), job_repository=_Jobs(),
            candidate_repository=_Candidates(), application_repository=applications,
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=InMemoryUserRepository(), settings=AppSettings()),
        )
        assert [a.application_id for a in await service.list_applications()] == ["app_1"]
        assert len(await service.list_applications(include_hidden=True)) == 2

    @pytest.mark.asyncio
    async def test_set_application_hidden_toggles(self):
        applications = _Applications()
        await applications.save(Application(application_id="app_1", job_id="job_1", candidate_id="cand_1"))
        service = AdminService(
            user_repository=InMemoryUserRepository(), job_repository=_Jobs(),
            candidate_repository=_Candidates(), application_repository=applications,
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=InMemoryUserRepository(), settings=AppSettings()),
        )
        hidden = await service.set_application_hidden("app_1", True)
        assert hidden.is_hidden is True

    @pytest.mark.asyncio
    async def test_set_application_hidden_unknown_is_not_found(self):
        service = _admin_service_full()
        with pytest.raises(NotFoundError):
            await service.set_application_hidden("does_not_exist", True)


# ---------------------------------------------------------------------------
# AdminService.get_metrics
# ---------------------------------------------------------------------------

class TestAdminServiceMetrics:
    @pytest.mark.asyncio
    async def test_counts_users_jobs_and_applications(self):
        user_repo = InMemoryUserRepository()
        await user_repo.save(UserRecord(
            user_id="u1", email="a@example.com", password_hash="h", user_type="candidate",
        ))
        await user_repo.save(UserRecord(
            user_id="u2", email="b@example.com", password_hash="h", user_type="recruiter",
        ))
        await user_repo.save(UserRecord(
            user_id="u3", email="c@example.com", password_hash="h", user_type="admin",
        ))

        jobs = _Jobs()
        await jobs.save(JobRecord(job_id="job_1", job=_bare_job("job_1")))
        await jobs.save(JobRecord(job_id="job_2", job=_bare_job("job_2")))
        await jobs.archive("job_2")

        applications = _Applications()
        await applications.save(Application(application_id="app_1", job_id="job_1", candidate_id="cand_1"))
        await applications.save(Application(
            application_id="app_2", job_id="job_1", candidate_id="cand_2", is_hidden=True,
        ))

        service = AdminService(
            user_repository=user_repo, job_repository=jobs,
            candidate_repository=_Candidates(), application_repository=applications,
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=user_repo, settings=AppSettings()),
        )
        metrics = await service.get_metrics()
        assert metrics["users"] == {"candidate": 1, "recruiter": 1, "admin": 1}
        assert metrics["jobs"] == {"active": 1, "hidden": 1}
        assert metrics["applications"] == {"total": 2, "hidden": 1}


# ---------------------------------------------------------------------------
# AdminService.get_system_status
# ---------------------------------------------------------------------------

class _FakeAsyncpgPool:
    def __init__(self, *, fail: bool = False):
        self._fail = fail

    async def fetchval(self, _query):
        if self._fail:
            raise ConnectionError("simulated DB outage")
        return 1


class _FakeConnectionPool:
    """Duck-types repositories.postgres.pool.PostgresConnectionPool's only
    method AdminService.get_system_status calls."""

    def __init__(self, *, fail: bool = False):
        self._asyncpg_pool = _FakeAsyncpgPool(fail=fail)

    async def get(self):
        return self._asyncpg_pool


class _FakeDispatcher:
    def __init__(self, running_count: int = 0):
        self.running_count = running_count


def _system_status_service(*, database_pool=None, evaluation_dispatcher=None, settings=None):
    user_repo = InMemoryUserRepository()
    settings = settings or AppSettings()
    return AdminService(
        user_repository=user_repo,
        job_repository=_Jobs(),
        candidate_repository=_Candidates(),
        application_repository=_Applications(),
        audit_log_repository=InMemoryAuditLogRepository(),
        auth_service=AuthService(user_repository=user_repo, settings=settings),
        settings=settings,
        database_pool=database_pool,
        evaluation_dispatcher=evaluation_dispatcher,
    )


class TestAdminServiceSystemStatus:
    @pytest.mark.asyncio
    async def test_raises_without_settings(self):
        user_repo = InMemoryUserRepository()
        service = AdminService(
            user_repository=user_repo,
            job_repository=_Jobs(),
            candidate_repository=_Candidates(),
            application_repository=_Applications(),
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=user_repo, settings=AppSettings()),
        )
        with pytest.raises(RuntimeError):
            await service.get_system_status()

    @pytest.mark.asyncio
    async def test_no_database_pool_reports_memory_persistence(self):
        service = _system_status_service()
        status = await service.get_system_status()
        assert status["persistence"] == "memory"
        assert status["database"]["connected"] is False
        assert status["database"]["error"]

    @pytest.mark.asyncio
    async def test_reachable_database_reports_connected_with_latency(self):
        service = _system_status_service(database_pool=_FakeConnectionPool())
        status = await service.get_system_status()
        assert status["persistence"] == "postgres"
        assert status["database"]["connected"] is True
        assert status["database"]["latency_ms"] is not None
        assert status["database"]["error"] is None

    @pytest.mark.asyncio
    async def test_unreachable_database_reports_disconnected_not_raise(self):
        service = _system_status_service(database_pool=_FakeConnectionPool(fail=True))
        status = await service.get_system_status()
        assert status["database"]["connected"] is False
        assert "simulated DB outage" in status["database"]["error"]

    @pytest.mark.asyncio
    async def test_reports_jwt_secret_placeholder_flag(self):
        placeholder_settings = AppSettings()
        real_settings = AppSettings(jwt_secret_key="a-real-generated-secret")

        placeholder_status = await _system_status_service(settings=placeholder_settings).get_system_status()
        real_status = await _system_status_service(settings=real_settings).get_system_status()

        assert placeholder_status["jwt_secret_is_placeholder"] is True
        assert real_status["jwt_secret_is_placeholder"] is False

    @pytest.mark.asyncio
    async def test_reports_evaluation_queue_running_count(self):
        service = _system_status_service(evaluation_dispatcher=_FakeDispatcher(running_count=3))
        status = await service.get_system_status()
        assert status["evaluation_queue"]["running"] == 3

    @pytest.mark.asyncio
    async def test_no_dispatcher_reports_zero_running(self):
        service = _system_status_service()
        status = await service.get_system_status()
        assert status["evaluation_queue"]["running"] == 0

    @pytest.mark.asyncio
    async def test_reports_provider_config(self):
        settings = AppSettings()
        service = _system_status_service(settings=settings)
        status = await service.get_system_status()
        assert status["providers"] == {
            "llm": settings.llm_provider,
            "embedding": settings.embedding_provider,
            "audio": settings.audio_provider,
            "tts": settings.tts_provider,
        }


# ---------------------------------------------------------------------------
# AuthService.issue_tokens_for_user
# ---------------------------------------------------------------------------

class TestIssueTokensForUser:
    @pytest.mark.asyncio
    async def test_issues_a_valid_access_and_refresh_token_pair(self):
        user_repo = InMemoryUserRepository()
        service = AuthService(user_repository=user_repo, settings=AppSettings())
        user, _a, _r = await service.signup(
            email="c@example.com", password="password123", user_type="candidate",
        )
        access_token, refresh_token = service.issue_tokens_for_user(user)

        principal = await service.verify_access_token(access_token)
        assert principal.subject_id == user.user_id

        new_access = await service.refresh_access_token(refresh_token)
        assert new_access


# ---------------------------------------------------------------------------
# AdminService.impersonate
# ---------------------------------------------------------------------------

class TestAdminServiceImpersonate:
    @pytest.mark.asyncio
    async def test_issues_tokens_for_the_target_and_writes_one_audit_log_row(self):
        user_repo = InMemoryUserRepository()
        auth_service = AuthService(user_repository=user_repo, settings=AppSettings())
        target, _a, _r = await auth_service.signup(
            email="target@example.com", password="password123", user_type="candidate",
        )
        audit_logs = InMemoryAuditLogRepository()
        service = AdminService(
            user_repository=user_repo, job_repository=_Jobs(),
            candidate_repository=_Candidates(), application_repository=_Applications(),
            audit_log_repository=audit_logs, auth_service=auth_service,
        )

        access_token, refresh_token = await service.impersonate("user_admin_1", target.user_id)

        principal = await auth_service.verify_access_token(access_token)
        assert principal.subject_id == target.user_id

        rows = await audit_logs.list_for_admin("user_admin_1")
        assert len(rows) == 1
        assert rows[0].action == "impersonate"
        assert rows[0].target_user_id == target.user_id

    @pytest.mark.asyncio
    async def test_unknown_target_is_not_found(self):
        service = _admin_service_full()
        with pytest.raises(NotFoundError):
            await service.impersonate("user_admin_1", "does_not_exist")

    @pytest.mark.asyncio
    async def test_inactive_target_is_rejected(self):
        user_repo = InMemoryUserRepository()
        auth_service = AuthService(user_repository=user_repo, settings=AppSettings())
        target, _a, _r = await auth_service.signup(
            email="target@example.com", password="password123", user_type="candidate",
        )
        await user_repo.save(target.model_copy(update={"is_active": False}))
        service = AdminService(
            user_repository=user_repo, job_repository=_Jobs(),
            candidate_repository=_Candidates(), application_repository=_Applications(),
            audit_log_repository=InMemoryAuditLogRepository(), auth_service=auth_service,
        )
        with pytest.raises(BadRequestError):
            await service.impersonate("user_admin_1", target.user_id)
