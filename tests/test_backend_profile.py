"""
Profile page backend: account-level profile fields on `users`, and the
GET/PATCH /auth/me + POST /auth/me/password endpoints.

Candidate resume data (skills, experience, education) is deliberately NOT
duplicated here - it stays owned by `CandidateRecord`/`ParsedResume` and is
only ever read, never written, through this module's endpoints. See
`repositories/interfaces.py`'s `CandidateRecord` docstring for why.

BYOK key storage is out of scope for this module - see the `byok` branch.
"""
import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.config import AppSettings, get_settings
from core.container import ServiceContainer
from core.security import JWTAuthProvider
from repositories.interfaces import UserRecord
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
from services.auth_service import AuthService
from services.evaluation_dispatcher import AsyncTaskEvaluationDispatcher


class TestUserRecordProfileFields:
    def test_profile_fields_default_to_none_and_round_trip(self):
        """A UserRecord built the way signup builds it today (no profile
        fields supplied) must still validate - these columns are additive,
        not required."""
        record = UserRecord(
            user_id="user_1",
            email="a@example.com",
            password_hash="hash",
            user_type="candidate",
        )
        assert record.full_name is None
        assert record.company_name is None

        updated = record.model_copy(update={"full_name": "Jane Doe", "phone": "555-0100"})
        assert updated.full_name == "Jane Doe"
        assert updated.phone == "555-0100"
        # Untouched fields still round-trip.
        assert updated.email == "a@example.com"


# ---------------------------------------------------------------------------
# AuthService.get_profile / update_profile / change_password
# ---------------------------------------------------------------------------

from core.errors import BadRequestError, NotFoundError, UnauthorizedError
from core.config import AppSettings


@pytest.fixture
def user_repo():
    return InMemoryUserRepository()


@pytest.fixture
def auth_service(user_repo):
    return AuthService(user_repository=user_repo, settings=AppSettings())


class TestAuthServiceProfile:
    async def _signed_up_user(self, service, user_type="candidate"):
        user, _access, _refresh = await service.signup(
            email=f"{user_type}@example.com", password="password123", user_type=user_type
        )
        return user

    @pytest.mark.asyncio
    async def test_get_profile_returns_the_stored_record(self, auth_service):
        user = await self._signed_up_user(auth_service)
        fetched = await auth_service.get_profile(user.user_id)
        assert fetched.user_id == user.user_id
        assert fetched.full_name is None

    @pytest.mark.asyncio
    async def test_get_profile_unknown_user_is_not_found(self, auth_service):
        with pytest.raises(NotFoundError):
            await auth_service.get_profile("user_does_not_exist")

    @pytest.mark.asyncio
    async def test_update_profile_applies_only_supplied_fields(self, auth_service):
        user = await self._signed_up_user(auth_service)
        updated = await auth_service.update_profile(
            user.user_id, {"full_name": "Jane Doe", "location": "Remote"}
        )
        assert updated.full_name == "Jane Doe"
        assert updated.location == "Remote"
        assert updated.headline is None  # untouched, not cleared

        updated_again = await auth_service.update_profile(user.user_id, {"headline": "Engineer"})
        assert updated_again.full_name == "Jane Doe"  # still there
        assert updated_again.headline == "Engineer"

    @pytest.mark.asyncio
    async def test_update_profile_explicit_null_clears_a_field(self, auth_service):
        user = await self._signed_up_user(auth_service)
        await auth_service.update_profile(user.user_id, {"bio": "Hello"})
        cleared = await auth_service.update_profile(user.user_id, {"bio": None})
        assert cleared.bio is None

    @pytest.mark.asyncio
    async def test_update_profile_cannot_touch_account_fields(self, auth_service):
        """email/user_type/is_active/password_hash are not in the request
        model at all (Task 4), but this pins the service layer too: even a
        caller that assembled the dict directly cannot use these keys to
        change protected fields."""
        user = await self._signed_up_user(auth_service)
        updated = await auth_service.update_profile(
            user.user_id, {"email": "changed@example.com", "full_name": "Jane"}
        )
        assert updated.email == user.email  # unchanged
        assert updated.full_name == "Jane"

    @pytest.mark.asyncio
    async def test_update_profile_rejects_recruiter_fields_from_a_candidate(self, auth_service):
        user = await self._signed_up_user(auth_service, user_type="candidate")
        with pytest.raises(BadRequestError):
            await auth_service.update_profile(user.user_id, {"company_name": "Acme"})

    @pytest.mark.asyncio
    async def test_update_profile_allows_recruiter_fields_for_a_recruiter(self, auth_service):
        user = await self._signed_up_user(auth_service, user_type="recruiter")
        updated = await auth_service.update_profile(user.user_id, {"company_name": "Acme"})
        assert updated.company_name == "Acme"

    @pytest.mark.asyncio
    async def test_update_profile_unknown_user_is_not_found(self, auth_service):
        with pytest.raises(NotFoundError):
            await auth_service.update_profile("user_does_not_exist", {"full_name": "X"})

    @pytest.mark.asyncio
    async def test_change_password_succeeds_with_correct_current_password(self, auth_service):
        user = await self._signed_up_user(auth_service)
        await auth_service.change_password(user.user_id, "password123", "newpassword456")
        # Login with the new password now works; the old one doesn't.
        logged_in, _, _ = await auth_service.login(user.email, "newpassword456")
        assert logged_in.user_id == user.user_id
        with pytest.raises(UnauthorizedError):
            await auth_service.login(user.email, "password123")

    @pytest.mark.asyncio
    async def test_change_password_rejects_wrong_current_password(self, auth_service):
        user = await self._signed_up_user(auth_service)
        with pytest.raises(UnauthorizedError):
            await auth_service.change_password(user.user_id, "wrongpassword", "newpassword456")

    @pytest.mark.asyncio
    async def test_change_password_unknown_user_is_not_found(self, auth_service):
        with pytest.raises(NotFoundError):
            await auth_service.change_password("user_does_not_exist", "a", "newpassword456")


# ---------------------------------------------------------------------------
# schemas/auth.py: UserProfileResponse / UpdateProfileRequest / ChangePasswordRequest
# ---------------------------------------------------------------------------

from schemas.auth import ChangePasswordRequest, UpdateProfileRequest, UserProfileResponse


class TestProfileSchemas:
    def test_user_profile_response_from_record_carries_no_password_hash(self):
        record = UserRecord(
            user_id="user_1",
            email="a@example.com",
            password_hash="secret-hash",
            user_type="candidate",
            full_name="Jane Doe",
        )
        response = UserProfileResponse.from_record(record)
        assert response.full_name == "Jane Doe"
        assert response.email == "a@example.com"
        assert "password_hash" not in response.model_dump()
        assert "secret-hash" not in response.model_dump_json()

    def test_update_profile_request_has_no_protected_fields(self):
        field_names = set(UpdateProfileRequest.model_fields.keys())
        assert field_names.isdisjoint({"email", "user_type", "is_active", "password_hash", "user_id"})

    def test_update_profile_request_exclude_unset_only_carries_supplied_fields(self):
        request = UpdateProfileRequest(full_name="Jane")
        assert request.model_dump(exclude_unset=True) == {"full_name": "Jane"}

    def test_change_password_request_rejects_short_new_password(self):
        with pytest.raises(Exception):
            ChangePasswordRequest(current_password="old12345", new_password="short")


# ---------------------------------------------------------------------------
# HTTP layer: GET/PATCH /auth/me, POST /auth/me/password
#
# Uses a real JWTAuthProvider over an in-memory UserRepository so these
# tests can prove per-user identity end to end (which user a token
# resolves to matters here, unlike the anonymous-principal tests
# elsewhere in this codebase). AUTH_ENABLED=true is safe with an in-memory
# backend as long as a real AuthProvider is installed - see
# core/lifespan.py's validate_startup_configuration.
# ---------------------------------------------------------------------------

def _authenticated_app():
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
    # Bypassing the lifespan's own build_default_container (core/lifespan.py)
    # is deliberate: that factory has no branch for "in-memory + real auth
    # provider", and adding one there would be a production wiring change
    # for a test-only need. TestClient below is used WITHOUT the `with`
    # context manager, so lifespan startup never runs and never overwrites
    # this container.
    app.state.container = container
    # core/security.py's get_principal/require_authenticated read
    # AppSettings via the process-wide `core.config.get_settings()` (an
    # lru_cache singleton), not through the container - so without this
    # override every request here would see whatever auth_enabled value
    # happened to be cached first (false, per tests/conftest.py), making
    # `require_authenticated` inert regardless of this app's own settings.
    # Overriding the dependency (rather than mutating the shared cache) is
    # local to this app instance and leaves the process-wide cache, and
    # every other test file relying on its default, untouched.
    app.dependency_overrides[get_settings] = lambda: settings
    return app, auth_service


@pytest.fixture
def authenticated_client():
    app, auth_service = _authenticated_app()
    client = TestClient(app)
    return client, auth_service


def _signup_and_get_token(client, email="candidate@example.com", user_type="candidate"):
    response = client.post(
        "/auth/signup",
        json={"email": email, "password": "password123", "user_type": user_type},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["user"]["user_id"], body["access_token"]


class TestGetMe:
    def test_returns_the_caller_own_profile(self, authenticated_client):
        client, _ = authenticated_client
        user_id, token = _signup_and_get_token(client)
        response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == user_id
        assert body["email"] == "candidate@example.com"
        assert body["user_type"] == "candidate"
        assert "password_hash" not in body

    def test_requires_authentication(self, authenticated_client):
        client, _ = authenticated_client
        response = client.get("/auth/me")
        assert response.status_code == 401


class TestPatchMe:
    def test_updates_supplied_fields_only(self, authenticated_client):
        client, _ = authenticated_client
        _user_id, token = _signup_and_get_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        response = client.patch("/auth/me", json={"full_name": "Jane Doe"}, headers=headers)
        assert response.status_code == 200
        assert response.json()["full_name"] == "Jane Doe"

        response = client.patch("/auth/me", json={"location": "Remote"}, headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["full_name"] == "Jane Doe"  # still set
        assert body["location"] == "Remote"

    def test_cannot_change_email_or_user_type(self, authenticated_client):
        client, _ = authenticated_client
        _user_id, token = _signup_and_get_token(client)
        headers = {"Authorization": f"Bearer {token}"}
        response = client.patch(
            "/auth/me",
            json={"email": "new@example.com", "user_type": "recruiter"},
            headers=headers,
        )
        # email/user_type are not fields on UpdateProfileRequest, so FastAPI
        # ignores them rather than erroring - the response proves neither
        # took effect.
        assert response.status_code == 200
        body = response.json()
        assert body["email"] == "candidate@example.com"
        assert body["user_type"] == "candidate"

    def test_rejects_recruiter_fields_from_a_candidate(self, authenticated_client):
        client, _ = authenticated_client
        _user_id, token = _signup_and_get_token(client, user_type="candidate")
        headers = {"Authorization": f"Bearer {token}"}
        response = client.patch("/auth/me", json={"company_name": "Acme"}, headers=headers)
        assert response.status_code == 400

    def test_allows_recruiter_fields_for_a_recruiter(self, authenticated_client):
        client, _ = authenticated_client
        _user_id, token = _signup_and_get_token(
            client, email="recruiter@example.com", user_type="recruiter"
        )
        headers = {"Authorization": f"Bearer {token}"}
        response = client.patch("/auth/me", json={"company_name": "Acme"}, headers=headers)
        assert response.status_code == 200
        assert response.json()["company_name"] == "Acme"

    def test_requires_authentication(self, authenticated_client):
        client, _ = authenticated_client
        response = client.patch("/auth/me", json={"full_name": "X"})
        assert response.status_code == 401


class TestChangePassword:
    def test_succeeds_with_correct_current_password(self, authenticated_client):
        client, _ = authenticated_client
        _user_id, token = _signup_and_get_token(client)
        headers = {"Authorization": f"Bearer {token}"}
        response = client.post(
            "/auth/me/password",
            json={"current_password": "password123", "new_password": "newpassword456"},
            headers=headers,
        )
        assert response.status_code == 204

        # Old password no longer works; new one does.
        login_old = client.post(
            "/auth/login", json={"email": "candidate@example.com", "password": "password123"}
        )
        assert login_old.status_code == 401
        login_new = client.post(
            "/auth/login", json={"email": "candidate@example.com", "password": "newpassword456"}
        )
        assert login_new.status_code == 200

    def test_rejects_wrong_current_password(self, authenticated_client):
        client, _ = authenticated_client
        _user_id, token = _signup_and_get_token(client)
        headers = {"Authorization": f"Bearer {token}"}
        response = client.post(
            "/auth/me/password",
            json={"current_password": "wrongpassword", "new_password": "newpassword456"},
            headers=headers,
        )
        assert response.status_code == 401

    def test_requires_authentication(self, authenticated_client):
        client, _ = authenticated_client
        response = client.post(
            "/auth/me/password",
            json={"current_password": "a", "new_password": "newpassword456"},
        )
        assert response.status_code == 401
