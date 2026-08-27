"""Database connection pooling and migration runner using asyncpg."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore

from core.logging import get_logger

logger = get_logger("db.connection")

_pool: Optional["asyncpg.Pool"] = None

def get_database_url() -> Optional[str]:
    """Retrieve database URL from environment."""
    return os.getenv("DATABASE_URL") or os.getenv("DB_CONNECTION_STRING")

async def get_db_pool() -> Optional["asyncpg.Pool"]:
    """Get active asyncpg connection pool."""
    global _pool
    if _pool is not None:
        return _pool
    
    url = get_database_url()
    if not url:
        return None
    
    if asyncpg is None:
        logger.warning("asyncpg is not installed. PostgreSQL connections are unavailable.")
        return None

    try:
        # Connect to Postgres
        _pool = await asyncpg.create_pool(dsn=url, min_size=1, max_size=10)
        logger.info("Database connection pool established.")
        return _pool
    except Exception as exc:
        logger.error(f"Failed to create database connection pool: {exc}")
        return None

async def run_migrations(pool: Optional["asyncpg.Pool"] = None) -> bool:
    """Run all SQL migrations in order."""
    p = pool or await get_db_pool()
    if p is None:
        logger.warning("No database pool available to run migrations.")
        return False

    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    if not migrations_dir.exists():
        logger.warning(f"Migrations directory {migrations_dir} not found.")
        return False

    sql_files = sorted(migrations_dir.glob("*.sql"))
    if not sql_files:
        logger.info("No SQL migrations found.")
        return True

    async with p.acquire() as conn:
        # Create schema migrations table if not exists
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS _schema_migrations (
                version VARCHAR(255) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)

        applied_rows = await conn.fetch("SELECT version FROM _schema_migrations;")
        applied = {row["version"] for row in applied_rows}

        for sql_file in sql_files:
            version = sql_file.name
            if version in applied:
                continue

            logger.info(f"Applying migration: {version}")
            sql_content = sql_file.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql_content)
                await conn.execute(
                    "INSERT INTO _schema_migrations (version) VALUES ($1);",
                    version
                )
            logger.info(f"Applied migration: {version}")

    return True

async def init_db() -> Optional["asyncpg.Pool"]:
    """Initialize database and run migrations."""
    pool = await get_db_pool()
    if pool is not None:
        await run_migrations(pool)
    return pool

async def close_db() -> None:
    """Close connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Database connection pool closed.")
