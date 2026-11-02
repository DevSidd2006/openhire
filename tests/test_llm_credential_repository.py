"""Tests for LLMCredentialRecord/LLMCredentialRepository (repositories/interfaces.py)
and both concrete implementations (repositories/memory.py, repositories/postgres/)."""
import os
from datetime import datetime, timezone

import pytest

from repositories.interfaces import CredentialStatus, LLMCredentialRecord
from repositories.memory import InMemoryLLMCredentialRepository

# Postgres-backed tests below follow the same skip-guard/fixture pattern as
# tests/test_postgres_repositories.py: skipped entirely unless
# TEST_DATABASE_URL is set to a real scratch database's DSN.
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")

_asyncpg = pytest.importorskip("asyncpg")

if TEST_DATABASE_URL:
    from repositories.postgres.llm_credential_repository import PostgresLLMCredentialRepository
    from repositories.postgres.pool import PostgresConnectionPool


def _record(**overrides):
    defaults = dict(
        user_id="user_1",
        provider="openai",
        model="gpt-4o",
        encrypted_key=b"ciphertext-bytes",
        key_hint="...ab12",
        status=CredentialStatus.ACTIVE,
    )
    defaults.update(overrides)
    return LLMCredentialRecord(**defaults)


class TestInMemoryLLMCredentialRepository:
    @pytest.mark.asyncio
    async def test_save_then_get_round_trips(self):
        repo = InMemoryLLMCredentialRepository()
        saved = await repo.save(_record())
        fetched = await repo.get("user_1")
        assert fetched is not None
        assert fetched.provider == "openai"
        assert fetched.encrypted_key == b"ciphertext-bytes"
        assert saved.user_id == fetched.user_id

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self):
        repo = InMemoryLLMCredentialRepository()
        assert await repo.get("nobody") is None

    @pytest.mark.asyncio
    async def test_multiple_credentials_per_user_are_preserved(self):
        repo = InMemoryLLMCredentialRepository()
        first = await repo.save(_record(provider="openai"))
        second = await repo.save(_record(provider="groq", created_at=datetime.now(timezone.utc)))
        fetched = await repo.get("user_1")
        rows = await repo.list_for_user("user_1")
        assert len(rows) == 2
        assert {r.provider for r in rows} == {"openai", "groq"}
        assert fetched.credential_id == first.credential_id
        updated = await repo.save(second.model_copy(update={"name": "renamed"}))
        assert updated.created_at == second.created_at

    @pytest.mark.asyncio
    async def test_delete_is_idempotent(self):
        repo = InMemoryLLMCredentialRepository()
        await repo.save(_record())
        await repo.delete("user_1")
        assert await repo.get("user_1") is None
        await repo.delete("user_1")  # no raise
        assert await repo.get("user_1") is None


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason=(
        "TEST_DATABASE_URL is not set - skipping "
        "PostgresLLMCredentialRepository contract tests. Set it to a "
        "scratch PostgreSQL database's DSN to run them."
    ),
)
class TestPostgresLLMCredentialRepository:
    @pytest.fixture(scope="module")
    def pool(self):
        return PostgresConnectionPool(TEST_DATABASE_URL)

    @pytest.mark.asyncio
    async def test_save_then_get_round_trips(self, pool):
        repo = PostgresLLMCredentialRepository(pool)
        saved = await repo.save(_record(user_id="user_pg_1"))
        fetched = await repo.get("user_pg_1")
        assert fetched.provider == "openai"
        assert fetched.encrypted_key == b"ciphertext-bytes"
        assert saved.user_id == "user_pg_1"

    @pytest.mark.asyncio
    async def test_multiple_credentials_per_user_are_preserved(self, pool):
        repo = PostgresLLMCredentialRepository(pool)
        await repo.save(_record(user_id="user_pg_2", provider="openai"))
        await repo.save(_record(user_id="user_pg_2", provider="groq"))
        fetched = await repo.list_for_user("user_pg_2")
        assert len(fetched) == 2
        assert {r.provider for r in fetched} == {"openai", "groq"}

    @pytest.mark.asyncio
    async def test_delete_is_idempotent(self, pool):
        repo = PostgresLLMCredentialRepository(pool)
        await repo.save(_record(user_id="user_pg_3"))
        await repo.delete("user_pg_3")
        assert await repo.get("user_pg_3") is None
        await repo.delete("user_pg_3")
