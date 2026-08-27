"""CLI runner for database migrations."""
import asyncio
import sys

from core.logging import configure_logging
from core.config import get_settings
from db.connection import get_db_pool, run_migrations, close_db

async def main():
    settings = get_settings()
    configure_logging(settings)
    print("Connecting to database and running migrations...")
    pool = await get_db_pool()
    if pool is None:
        print("ERROR: Could not establish a database connection. Please check DATABASE_URL / DB_CONNECTION_STRING.")
        sys.exit(1)
    
    success = await run_migrations(pool)
    await close_db()
    if success:
        print("Migrations completed successfully.")
    else:
        print("Migrations failed.")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
