"""
PostgreSQL implementation of AuditLogRepository - the impersonation
accountability trail. See repositories/postgres/schema.sql's `audit_logs`
table.
"""
from __future__ import annotations

import asyncpg

from repositories.interfaces import AuditLogRecord, AuditLogRepository
from repositories.postgres.pool import PostgresConnectionPool


def _audit_log_from_row(row: asyncpg.Record) -> AuditLogRecord:
    return AuditLogRecord(
        log_id=row["log_id"],
        admin_id=row["admin_id"],
        action=row["action"],
        target_user_id=row["target_user_id"],
        created_at=row["created_at"],
    )


class PostgresAuditLogRepository(AuditLogRepository):
    """Durable, append-only storage for `AuditLogRecord`."""

    def __init__(self, pool: PostgresConnectionPool) -> None:
        self._pool = pool

    async def save(self, record: AuditLogRecord) -> AuditLogRecord:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO audit_logs (log_id, admin_id, action, target_user_id, created_at)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING log_id, admin_id, action, target_user_id, created_at
                """,
                record.log_id,
                record.admin_id,
                record.action,
                record.target_user_id,
                record.created_at,
            )
        return _audit_log_from_row(row)

    async def list_for_admin(self, admin_id: str) -> list[AuditLogRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM audit_logs WHERE admin_id = $1 ORDER BY created_at DESC",
                admin_id,
            )
        return [_audit_log_from_row(row) for row in rows]


__all__ = ["PostgresAuditLogRepository"]
