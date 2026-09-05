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

from repositories.interfaces import UserRecord
from repositories.memory import InMemoryUserRepository
from services.auth_service import AuthService


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
