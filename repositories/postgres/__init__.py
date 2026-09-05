"""
Real PostgreSQL-backed implementations of the persistence contracts defined
in repositories/interfaces.py.

This is the replacement `repositories/memory.py`'s module docstring points
to: "Write repositories/<backend>.py implementing [the repositories] from
repositories/interfaces.py, then change the two construction lines in
core/container.py." This package is that file (as a package rather than a
single module, purely so the connection pool, the DDL and the six
implementations each get their own readable file) - see
`core/container.py:build_default_container` for the actual swap, which is
gated on `AppSettings.database_url` being set so every existing test that
does not configure one keeps getting the in-memory stubs unchanged.

`repositories/postgres/schema.sql` is the DDL every implementation here
assumes already exists. It is not applied automatically by any code in this
package - running it (e.g. `psql "$DATABASE_URL" -f repositories/postgres/schema.sql`)
is a deployment step, not something a repository constructor should do on
every process start.
"""
from __future__ import annotations

from repositories.postgres.pool import PostgresConnectionPool
from repositories.postgres.repository import (
    PostgresRubricRepository,
    PostgresApplicationRepository,
    PostgresBugReportRepository,
    PostgresCandidateRepository,
    PostgresEvaluationRepository,
    PostgresJobRepository,
    PostgresSessionRepository,
    PostgresTranscriptRepository,
)
from repositories.postgres.user_repository import PostgresUserRepository

# Named so it can be asserted on and logged, the same way
# repositories/memory.py's EPHEMERAL_BACKEND_NAME is.
POSTGRES_BACKEND_NAME = "PostgreSQL (repositories/postgres)"

__all__ = [
    "PostgresRubricRepository",
    "POSTGRES_BACKEND_NAME",
    "PostgresConnectionPool",
    "PostgresApplicationRepository",
    "PostgresBugReportRepository",
    "PostgresCandidateRepository",
    "PostgresEvaluationRepository",
    "PostgresJobRepository",
    "PostgresSessionRepository",
    "PostgresTranscriptRepository",
    "PostgresUserRepository",
]
