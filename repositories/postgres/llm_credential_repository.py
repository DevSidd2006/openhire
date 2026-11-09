"""PostgreSQL implementation of LLMCredentialRepository (BYOK). See
repositories/postgres/schema.sql's `user_llm_credentials` table.

Kept in its own module (like PostgresAuditLogRepository /
PostgresUserRepository) rather than folded into repository.py, since it is
one focused table with no relations to the six repositories already there.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import asyncpg

from repositories.interfaces import CredentialStatus, LLMCredentialRecord, LLMCredentialRepository
from repositories.postgres.pool import PostgresConnectionPool


def _credential_from_row(row: asyncpg.Record) -> LLMCredentialRecord:
    return LLMCredentialRecord(
        user_id=row["user_id"],
        provider=row["provider"],
        model=row["model"],
        encrypted_key=bytes(row["encrypted_key"]),
        key_hint=row["key_hint"],
        status=CredentialStatus(row["status"]),
        last_error=row["last_error"],
        last_error_at=row["last_error_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PostgresLLMCredentialRepository(LLMCredentialRepository):
    """Durable storage for `LLMCredentialRecord`. One row per user."""

    def __init__(self, pool: PostgresConnectionPool) -> None:
        self._pool = pool

    async def save(self, record: LLMCredentialRecord) -> LLMCredentialRecord:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO user_llm_credentials
                    (user_id, provider, model, encrypted_key, key_hint, status,
                     last_error, last_error_at, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
                ON CONFLICT (user_id) DO UPDATE
                    SET provider = EXCLUDED.provider,
                        model = EXCLUDED.model,
                        encrypted_key = EXCLUDED.encrypted_key,
                        key_hint = EXCLUDED.key_hint,
                        status = EXCLUDED.status,
                        last_error = EXCLUDED.last_error,
                        last_error_at = EXCLUDED.last_error_at,
                        updated_at = now()
                RETURNING user_id, provider, model, encrypted_key, key_hint, status,
                          last_error, last_error_at, created_at, updated_at
                """,
                record.user_id,
                record.provider,
                record.model,
                record.encrypted_key,
                record.key_hint,
                record.status.value,
                record.last_error,
                record.last_error_at,
                record.created_at,
            )
        return _credential_from_row(row)

    async def get(self, user_id: str) -> Optional[LLMCredentialRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM user_llm_credentials WHERE user_id = $1", user_id
            )
        return _credential_from_row(row) if row is not None else None

    async def delete(self, user_id: str) -> None:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_llm_credentials WHERE user_id = $1", user_id
            )

    async def save_platform_health(self, provider: str, result: dict) -> None:
        """Persist the latest platform-provider health check.

        Health probes serialize timestamps for API responses, while asyncpg
        expects real ``datetime`` values for timestamptz parameters. Convert
        local copies here so callers retain their original result payload.
        """

        def as_datetime(value: object) -> object:
            if not isinstance(value, str):
                return value
            return datetime.fromisoformat(value.replace("Z", "+00:00"))

        last_tested_at = as_datetime(result.get("last_tested_at"))
        last_successful_test_at = as_datetime(result.get("last_successful_test_at"))
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO platform_llm_provider_health
                    (provider, model, status, auth_status, last_tested_at,
                     last_successful_test_at, error_code, message, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
                ON CONFLICT (provider) DO UPDATE
                    SET model = EXCLUDED.model,
                        status = EXCLUDED.status,
                        auth_status = EXCLUDED.auth_status,
                        last_tested_at = EXCLUDED.last_tested_at,
                        last_successful_test_at = COALESCE(
                            EXCLUDED.last_successful_test_at,
                            platform_llm_provider_health.last_successful_test_at
                        ),
                        error_code = EXCLUDED.error_code,
                        message = EXCLUDED.message,
                        updated_at = now()
                """,
                provider,
                result.get("model"),
                result.get("status"),
                result.get("auth_status"),
                last_tested_at,
                last_successful_test_at,
                result.get("error_code"),
                result.get("message"),
            )

    async def get_platform_health(self) -> list[dict]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT provider, model, status, auth_status, last_tested_at,
                       last_successful_test_at, error_code, message
                FROM platform_llm_provider_health
                ORDER BY provider
                """
            )
        return [dict(row) for row in rows]
