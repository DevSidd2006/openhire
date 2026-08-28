"""
Lazy asyncpg connection pool shared by every Postgres-backed repository.

`core.container.build_default_container` is a synchronous function, called
both from a synchronous dependency resolver (core/dependencies.py) and from
inside an async lifespan handler without being awaited (core/lifespan.py) -
so nothing at container-construction time may perform I/O or require a
running event loop. `PostgresConnectionPool` defers opening the real
`asyncpg.Pool` until the first `async` call any repository makes, guarded by
an `asyncio.Lock` - the same double-checked-locking shape
`repositories/memory.py`'s dict repositories already use for their own
concurrency guarantee (there, guarding a dict mutation; here, guarding
one-time pool creation).

One instance of this class is created once in `core/container.py` and
handed to all six Postgres repository implementations, so the process opens
exactly one connection pool regardless of how many repository objects exist
- mirroring `evaluation_dispatcher`'s "constructed once, not per-request"
requirement in that same file.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

import asyncpg


async def _init_connection(conn: "asyncpg.Connection") -> None:
    """Teach every pooled connection to hand back plain Python dicts/lists
    for `jsonb` columns instead of raw JSON text, and to accept the same
    shape on the way in. Every repository in this package works with
    `model_dump(mode="json")` dicts and `model_validate(...)` on the way
    back out - never with JSON strings - because of this codec."""
    await conn.set_type_codec(
        "jsonb",
        schema="pg_catalog",
        encoder=json.dumps,
        decoder=json.loads,
        format="text",
    )


class PostgresConnectionPool:
    """Owns exactly one `asyncpg.Pool` for the whole process, opened lazily
    on first use rather than at construction time."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: Optional[asyncpg.Pool] = None
        self._lock = asyncio.Lock()

    async def get(self) -> "asyncpg.Pool":
        """The shared pool, opening it on the first call from any
        repository. Safe to call concurrently - only the first caller pays
        the connection-setup cost; everyone else waits on the same lock and
        then reads the same pool."""
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    self._dsn,
                    min_size=self._min_size,
                    max_size=self._max_size,
                    init=_init_connection,
                )
        return self._pool

    async def aclose(self) -> None:
        """Release the pool at shutdown. A no-op if it was never opened -
        e.g. a process that started and stopped without ever handling a
        request that touched a repository."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


__all__ = ["PostgresConnectionPool"]
