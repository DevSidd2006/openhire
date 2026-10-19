"""
Email case-insensitivity for user accounts.

`PostgresUserRepository` stores emails lowercase and looks them up with
`LOWER(email) = LOWER($1)`, so "Alice@example.com" and "alice@example.com"
are the same account. `InMemoryUserRepository` - the default for local
development and the test suite - must behave the same way, otherwise
signup/login semantics silently change depending on whether DATABASE_URL
is set.
"""
import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.config import AppSettings
from core.errors import ConflictError
from repositories.interfaces import UserRecord
from repositories.memory import InMemoryUserRepository
from services.auth_service import AuthService


def _user(user_id="user_1", email="Alice@Example.com"):
    return UserRecord(
        user_id=user_id,
        email=email,
        password_hash="hash",
        user_type="candidate",
    )


class TestInMemoryUserRepositoryEmailCase:
    @pytest.mark.asyncio
    async def test_save_stores_email_lowercase(self):
        repo = InMemoryUserRepository()
        stored = await repo.save(_user())
        assert stored.email == "alice@example.com"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "lookup", ["alice@example.com", "ALICE@EXAMPLE.COM", "Alice@Example.com"]
    )
    async def test_get_by_email_is_case_insensitive(self, lookup):
        repo = InMemoryUserRepository()
        await repo.save(_user())
        found = await repo.get_by_email(lookup)
        assert found is not None
        assert found.user_id == "user_1"

    @pytest.mark.asyncio
    async def test_get_by_email_unknown_still_returns_none(self):
        repo = InMemoryUserRepository()
        await repo.save(_user())
        assert await repo.get_by_email("bob@example.com") is None


class TestAuthServiceEmailCase:
    @pytest.fixture
    def service(self):
        return AuthService(user_repository=InMemoryUserRepository(), settings=AppSettings())

    @pytest.mark.asyncio
    async def test_login_accepts_different_casing_from_signup(self, service):
        signed_up, _, _ = await service.signup(
            email="Alice@Example.com", password="password123", user_type="candidate"
        )
        logged_in, _, _ = await service.login(email="alice@example.com", password="password123")
        assert logged_in.user_id == signed_up.user_id

    @pytest.mark.asyncio
    async def test_signup_rejects_same_email_with_different_casing(self, service):
        await service.signup(
            email="alice@example.com", password="password123", user_type="candidate"
        )
        with pytest.raises(ConflictError):
            await service.signup(
                email="ALICE@example.com", password="password123", user_type="recruiter"
            )


class TestAuthRoutesEmailCase:
    @pytest.fixture
    def client(self):
        with TestClient(create_app()) as c:
            yield c

    def test_signup_then_login_with_different_casing(self, client):
        signup = client.post(
            "/auth/signup",
            json={"email": "Alice@Example.com", "password": "password123", "user_type": "candidate"},
        )
        assert signup.status_code == 200, signup.text
        assert signup.json()["user"]["email"] == "alice@example.com"

        login = client.post(
            "/auth/login", json={"email": "alice@example.com", "password": "password123"}
        )
        assert login.status_code == 200, login.text
        assert login.json()["user"]["user_id"] == signup.json()["user"]["user_id"]

    def test_duplicate_signup_with_different_casing_conflicts(self, client):
        first = client.post(
            "/auth/signup",
            json={"email": "alice@example.com", "password": "password123", "user_type": "candidate"},
        )
        assert first.status_code == 200, first.text

        second = client.post(
            "/auth/signup",
            json={"email": "ALICE@example.com", "password": "password123", "user_type": "candidate"},
        )
        assert second.status_code == 409, second.text
