"""
PostgreSQL implementation of UserRepository for user account persistence.

Implements the UserRepository ABC from repositories/interfaces.py, backing
user authentication and account management with the `users` table defined in
repositories/postgres/schema.sql.

Email lookups are case-insensitive; emails are stored lowercase to enforce
this at the schema level. The save method is idempotent and preserves
created_at on update.
"""
from __future__ import annotations

from typing import Optional

import asyncpg

from repositories.interfaces import UserRecord, UserRepository
from repositories.postgres.pool import PostgresConnectionPool


def _user_record_from_row(row: asyncpg.Record) -> UserRecord:
    """Convert a database row to a UserRecord domain object."""
    return UserRecord(
        user_id=row["user_id"],
        email=row["email"],
        password_hash=row["password_hash"],
        user_type=row["user_type"],
        is_active=row["is_active"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PostgresUserRepository(UserRepository):
    """Durable storage for `UserRecord`. See
    repositories/postgres/schema.sql's `users` table."""

    def __init__(self, pool: PostgresConnectionPool) -> None:
        self._pool = pool

    async def save(self, record: UserRecord) -> UserRecord:
        """Insert or update by user_id. Idempotent, preserves created_at.

        Stores email in lowercase for case-insensitive lookups. Sets
        updated_at to the current timestamp on every write.
        """
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users
                    (user_id, email, password_hash, user_type, is_active, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, now())
                ON CONFLICT (user_id) DO UPDATE
                    SET email = EXCLUDED.email,
                        password_hash = EXCLUDED.password_hash,
                        user_type = EXCLUDED.user_type,
                        is_active = EXCLUDED.is_active,
                        updated_at = now()
                RETURNING user_id, email, password_hash, user_type, is_active, created_at, updated_at
                """,
                record.user_id,
                record.email.lower(),
                record.password_hash,
                record.user_type,
                record.is_active,
                record.created_at,
            )
        return _user_record_from_row(row)

    async def get_by_email(self, email: str) -> Optional[UserRecord]:
        """Fetch user by email. Case-insensitive lookup.

        Returns None if not found. Normalizes the input email to lowercase
        for lookup consistency.
        """
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE LOWER(email) = LOWER($1)",
                email,
            )
        return _user_record_from_row(row) if row is not None else None

    async def get_by_id(self, user_id: str) -> Optional[UserRecord]:
        """Fetch user by user_id.

        Returns None if not found. Never raises for absence.
        """
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        return _user_record_from_row(row) if row is not None else None


__all__ = ["PostgresUserRepository"]
