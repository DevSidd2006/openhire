# Admin Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third account type (`admin`) and an `/admin`-gated console covering user management, content moderation (jobs/candidates/applications), read-only platform metrics, and audited support impersonation.

**Architecture:** A new `require_admin` FastAPI dependency (mirrors `require_authenticated`, adds a fresh `UserRepository` lookup so a role change or deactivation takes effect immediately rather than waiting for token expiry). A new `services/admin_service.py` aggregates the existing `UserRepository`/`JobRepository`/`CandidateRepository`/`ApplicationRepository` plus a new `AuditLogRepository`, following the same "thin route, real logic in a service" split every other router in this codebase uses. New moderation flags (`CandidateRecord.is_hidden`, `Application.is_hidden`) reuse the existing `JobRecord.is_active` pattern; every repository method that lists these records by default excludes hidden/inactive rows, with an explicit `include_hidden`/`include_archived` opt-in for the admin views. A single new page, `pages/admin.html`, tabs Users/Moderation/Metrics, linked from the navbar dropdown only for `user_type === 'admin'`.

**Tech Stack:** FastAPI, Pydantic v2, `asyncpg` (Postgres), vanilla JS/HTML/CSS, `pytest` + `fastapi.testclient.TestClient`.

**Spec:** `docs/superpowers/specs/2026-09-06-admin-console-design.md`

## Global Constraints

- No permission granularity within "admin" — any admin can do everything an admin can do (spec §2).
- No admin self-service signup, ever — `POST /auth/signup`'s `user_type` pattern stays `^(candidate|recruiter)$` (already true today, `schemas/auth.py:22` — confirmed, not changed) and this plan never loosens it (spec §2, §4).
- No audit-log viewer UI in phase 1 — the log is written and queryable via the repository/DB directly (spec §2).
- No new fields on `UserRecord` for the admin role itself — an admin has no admin-specific profile data (spec §3.1).
- A hidden-by-admin job is indistinguishable from a recruiter-archived one — both flow through the existing `JobRecord.is_active` field and `JobRepository.archive` (spec §3.2, §8).
- Every route under `/admin` is gated by `require_admin` (`core/security.py`), which returns 403 (not 404) for an authenticated-but-non-admin caller (spec §5).
- `require_admin` resolves the caller's CURRENT `UserRecord.user_type` from the repository on every call, not only the JWT claim — a demoted/deactivated admin's still-valid access token must not keep working (spec §5, mirrors `AuthService.login`'s existing `is_active` check at `services/auth_service.py:146`).
- `PATCH /admin/users/{user_id}` rejects any `user_type` outside `{'candidate','recruiter','admin'}` with 400 (spec §5.1).
- Impersonation validates the target exists and is active, issues a normal token pair via the existing JWT issuance path, and writes exactly one `AuditLogRecord` per call (spec §5.4, §7).
- Follow existing patterns: `AppError` subclasses for errors (`core/errors.py`), routes stay thin and delegate to services, `apiRequest()` in `pages/js/app.js` for all frontend calls, `ConfigDict(frozen=False)` on mutable repository records matching every other record in `repositories/interfaces.py`.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `repositories/interfaces.py` | Modify | `AuditLogRecord`/`AuditLogRepository`; `is_hidden` on `CandidateRecord`; `set_hidden`/`include_hidden` on `CandidateRepository`; `restore` on `JobRepository`; `list_all`/`set_hidden`/`include_hidden` on `ApplicationRepository`; `list_users` on `UserRepository` |
| `schemas/application.py` | Modify | `is_hidden: bool = False` on `Application` |
| `repositories/memory.py` | Modify | `InMemoryAuditLogRepository`; the same interface changes as above, in-memory |
| `repositories/postgres/schema.sql` | Modify | `audit_logs` table; `candidates.is_hidden`, `applications.is_hidden` columns |
| `repositories/postgres/audit_log_repository.py` | Create | `PostgresAuditLogRepository` |
| `repositories/postgres/repository.py` | Modify | `restore`/`set_hidden`/`list_all`/`include_hidden` on the Postgres job/candidate/application repositories |
| `repositories/postgres/user_repository.py` | Modify | `list_users` |
| `repositories/postgres/__init__.py` | Modify | Export `PostgresAuditLogRepository` |
| `core/security.py` | Modify | `PrincipalType.ADMIN`; `require_admin` dependency |
| `core/container.py` | Modify | `audit_log_repository` field + both construction branches |
| `core/dependencies.py` | Modify | `get_audit_log_repository`, `get_admin_service` |
| `services/auth_service.py` | Modify | Admin `user_type` mapping/scopes; public `issue_tokens_for_user` |
| `services/admin_service.py` | Create | User management, moderation, metrics, impersonation use cases |
| `services/candidate_service.py` | Modify | `list_candidates(include_hidden=False)` passthrough |
| `schemas/admin.py` | Create | Admin request/response schemas |
| `api/routes/admin.py` | Create | `/admin` router |
| `api/routes/candidates.py` | Modify | `GET /candidates` excludes hidden by default |
| `api/app.py` | Modify | Register `admin_router` |
| `scripts/promote_admin.py` | Create | Operator script: promote an existing user to admin |
| `pages/admin.html` | Create | Users / Moderation / Metrics tabs |
| `pages/js/app.js` | Modify | `renderNavbar` admin link; admin API helpers |
| `tests/test_backend_admin.py` | Create | Backend tests for every new endpoint/behavior |

---

## Task 1: `PrincipalType.ADMIN` and admin `user_type` mapping

**Files:**
- Modify: `core/security.py` (`PrincipalType`)
- Modify: `services/auth_service.py` (`_map_user_type_to_principal_type`, `_get_scopes_for_user_type`)
- Test: `tests/test_backend_admin.py` (new file, starts here)

**Interfaces:**
- Produces: `PrincipalType.ADMIN`, used by `require_admin` (Task 5); `AuthService._get_scopes_for_user_type("admin")` returns `frozenset(["admin:read", "admin:write"])`, used nowhere yet but keeping the existing scope-per-role pattern consistent.

- [ ] **Step 1: Write the failing test**

Create `tests/test_backend_admin.py`:

```python
"""
Admin console backend: user_type == 'admin', require_admin, the audit log,
moderation flags, and the /admin router (user management, moderation,
metrics, impersonation).

No self-service admin signup exists anywhere - see
schemas/auth.py:SignupRequest's user_type pattern, unchanged by this module.
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from core.security import PrincipalType
from services.auth_service import AuthService
from repositories.memory import InMemoryUserRepository
from core.config import AppSettings


class TestAdminPrincipalType:
    def test_verify_access_token_maps_admin_user_type_to_admin_principal(self):
        settings = AppSettings()
        service = AuthService(user_repository=InMemoryUserRepository(), settings=settings)
        assert service._map_user_type_to_principal_type("admin") is PrincipalType.ADMIN

    def test_admin_gets_admin_scopes(self):
        settings = AppSettings()
        service = AuthService(user_repository=InMemoryUserRepository(), settings=settings)
        scopes = service._get_scopes_for_user_type("admin")
        assert "admin:read" in scopes
        assert "admin:write" in scopes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_admin.py -v`
Expected: FAIL — `AttributeError: ADMIN` (no such `PrincipalType` member) or the mapping still returns `ANONYMOUS`.

- [ ] **Step 3: Add `PrincipalType.ADMIN`**

In `core/security.py`, extend the enum:

```python
class PrincipalType(str, Enum):
    """Who a request is acting as.

    Four kinds the product has: the recruiter using the dashboard, the
    candidate following an interview link, the service itself (a background
    evaluation worker calling internally), and - as of the admin console -
    the operator account that manages users/content/impersonation. ANONYMOUS
    is the fifth, meaning "no credential was presented".
    """

    ANONYMOUS = "anonymous"
    CANDIDATE = "candidate"
    RECRUITER = "recruiter"
    SERVICE = "service"
    ADMIN = "admin"
```

- [ ] **Step 4: Wire `user_type == 'admin'` through `AuthService`**

In `services/auth_service.py`, update `_map_user_type_to_principal_type`:

```python
    @staticmethod
    def _map_user_type_to_principal_type(user_type: str) -> PrincipalType:
        """Map user_type string to PrincipalType enum.

        Args:
            user_type: User type string ('candidate', 'recruiter', or 'admin').

        Returns:
            Corresponding PrincipalType enum value.
        """
        if user_type == "candidate":
            return PrincipalType.CANDIDATE
        elif user_type == "recruiter":
            return PrincipalType.RECRUITER
        elif user_type == "admin":
            return PrincipalType.ADMIN
        else:
            # Default to ANONYMOUS for unknown types
            return PrincipalType.ANONYMOUS
```

And `_get_scopes_for_user_type`:

```python
    @staticmethod
    def _get_scopes_for_user_type(user_type: str) -> frozenset[str]:
        """Get OAuth scopes for a user type.

        Args:
            user_type: User type string ('candidate', 'recruiter', or 'admin').

        Returns:
            Frozenset of scopes appropriate for the user type.
        """
        if user_type == "recruiter":
            return frozenset(["recruiter:read", "recruiter:write"])
        elif user_type == "candidate":
            return frozenset(["candidate:read"])
        elif user_type == "admin":
            return frozenset(["admin:read", "admin:write"])
        else:
            return frozenset()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_backend_admin.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add core/security.py services/auth_service.py tests/test_backend_admin.py
git commit -m "feat(admin): add PrincipalType.ADMIN and map user_type='admin' through AuthService"
```

---

## Task 2: `AuditLogRecord`/`AuditLogRepository` (interfaces + in-memory)

**Files:**
- Modify: `repositories/interfaces.py`
- Modify: `repositories/memory.py`
- Test: `tests/test_backend_admin.py`

**Interfaces:**
- Produces: `AuditLogRecord(log_id, admin_id, action, target_user_id, created_at)`; `AuditLogRepository.save(record) -> AuditLogRecord`, `AuditLogRepository.list_for_admin(admin_id) -> list[AuditLogRecord]`; `InMemoryAuditLogRepository`. Used by Task 3 (Postgres) and Task 18 (`AdminService.impersonate`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backend_admin.py`:

```python
# ---------------------------------------------------------------------------
# AuditLogRecord / AuditLogRepository
# ---------------------------------------------------------------------------

from repositories.interfaces import AuditLogRecord
from repositories.memory import InMemoryAuditLogRepository


class TestAuditLogRepository:
    @pytest.mark.asyncio
    async def test_save_and_list_for_admin(self):
        repo = InMemoryAuditLogRepository()
        record = AuditLogRecord(
            log_id="log_1", admin_id="user_admin", action="impersonate",
            target_user_id="user_target",
        )
        saved = await repo.save(record)
        assert saved.log_id == "log_1"

        rows = await repo.list_for_admin("user_admin")
        assert len(rows) == 1
        assert rows[0].target_user_id == "user_target"

        assert await repo.list_for_admin("user_someone_else") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_admin.py -v`
Expected: FAIL — `ImportError: cannot import name 'AuditLogRecord'`

- [ ] **Step 3: Add `AuditLogRecord`/`AuditLogRepository` to `repositories/interfaces.py`**

Add after the `UserRepository` class (currently ending around line 497), before `class ApplicationRepository`:

```python
class AuditLogRecord(BaseModel):
    """One accountability record for an admin action.

    Phase 1 logs exactly one action, impersonation - see
    `AdminService.impersonate` (services/admin_service.py). `action` is
    free-text rather than an enum so a future admin action (e.g.
    "deactivate_user") can log through this same table with no migration -
    see this record's own justification in the design spec (§3.3).
    """

    model_config = ConfigDict(frozen=False)

    log_id: str
    admin_id: str
    action: str
    target_user_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AuditLogRepository(ABC):
    """Durable storage for `AuditLogRecord`. Append-only: no update/delete
    method exists because an accountability log must not be editable by the
    thing it is holding accountable."""

    @abstractmethod
    async def save(self, record: AuditLogRecord) -> AuditLogRecord:
        """Insert one record. Never upserts by `log_id` - every call is a
        new row, unlike every other repository's save()."""

    @abstractmethod
    async def list_for_admin(self, admin_id: str) -> list[AuditLogRecord]:
        """Every action logged for one admin, newest first."""
```

Add both names to `__all__` (currently lines 682-703):

```python
__all__ = [
    "Application",
    "ApplicationRepository",
    "AuditLogRecord",
    "AuditLogRepository",
    "BugReportRecord",
    "BugReportRepository",
    "BugSeverity",
    "BugStatus",
    "CandidateRecord",
    "CandidateRepository",
    "EvaluationJob",
    "EvaluationRepository",
    "EvaluationStatus",
    "JobRecord",
    "JobRepository",
    "RepositoryError",
    "SessionRecord",
    "SessionRepository",
    "SessionRuntimeRegistry",
    "TranscriptRepository",
    "UserRecord",
    "UserRepository",
]
```

- [ ] **Step 4: Add `InMemoryAuditLogRepository` to `repositories/memory.py`**

Add the import (extend the existing `from repositories.interfaces import (...)` block):

```python
from repositories.interfaces import (
    RubricRepository,
    Application,
    ApplicationRepository,
    AuditLogRecord,
    AuditLogRepository,
    BugReportRecord,
    BugReportRepository,
    CandidateRecord,
    CandidateRepository,
    EvaluationJob,
    EvaluationRepository,
    JobRecord,
    JobRepository,
    RepositoryError,
    SessionRecord,
    SessionRepository,
    TranscriptRepository,
    UserRecord,
    UserRepository,
)
```

Add the class after `InMemoryUserRepository` (currently ending around line 383), before the module's `__all__`:

```python
class InMemoryAuditLogRepository(AuditLogRepository):
    """List-backed `AuditLogRepository`. TEMPORARY - see module docstring."""

    def __init__(self) -> None:
        self._records: List[AuditLogRecord] = []
        self._lock = asyncio.Lock()

    async def save(self, record: AuditLogRecord) -> AuditLogRecord:
        async with self._lock:
            self._records.append(record)
            return record

    async def list_for_admin(self, admin_id: str) -> List[AuditLogRecord]:
        async with self._lock:
            matches = [r for r in self._records if r.admin_id == admin_id]
        return sorted(matches, key=lambda r: r.created_at, reverse=True)
```

Add `"InMemoryAuditLogRepository"` to the module's `__all__` list (currently lines 385-394).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_backend_admin.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add repositories/interfaces.py repositories/memory.py tests/test_backend_admin.py
git commit -m "feat(admin): add AuditLogRecord/AuditLogRepository and its in-memory implementation"
```

---

## Task 3: `PostgresAuditLogRepository` + container wiring

**Files:**
- Modify: `repositories/postgres/schema.sql`
- Create: `repositories/postgres/audit_log_repository.py`
- Modify: `repositories/postgres/__init__.py`
- Modify: `core/container.py`
- Modify: `core/dependencies.py`
- Test: `tests/test_backend_admin.py` (regression run only — no live Postgres in this environment, same convention as the profile-page plan's Task 2)

**Interfaces:**
- Consumes: `AuditLogRecord`/`AuditLogRepository` (Task 2).
- Produces: `ServiceContainer.audit_log_repository`, `core.dependencies.get_audit_log_repository(request) -> AuditLogRepository`, used by Task 18's `AdminService`.

- [ ] **Step 1: Add the `audit_logs` table**

In `repositories/postgres/schema.sql`, add after the `bug_reports` table (end of file):

```sql
-- ---------------------------------------------------------------------
-- audit_logs — AuditLogRecord (repositories/interfaces.py): accountability
-- trail for admin actions. Phase 1 writes exactly one action
-- ("impersonate") - see services/admin_service.py:AdminService.impersonate.
-- Append-only: no UPDATE/DELETE path exists anywhere in the backend for
-- this table.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
    log_id            text PRIMARY KEY,
    admin_id          text NOT NULL REFERENCES users (user_id),
    action            text NOT NULL,
    target_user_id    text NOT NULL REFERENCES users (user_id),
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- Backs AuditLogRepository.list_for_admin(admin_id): "newest first".
CREATE INDEX IF NOT EXISTS idx_audit_logs_admin_id_created_at
    ON audit_logs (admin_id, created_at DESC);
```

- [ ] **Step 2: Create `repositories/postgres/audit_log_repository.py`**

```python
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
```

- [ ] **Step 3: Export it from `repositories/postgres/__init__.py`**

```python
from repositories.postgres.audit_log_repository import PostgresAuditLogRepository
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

POSTGRES_BACKEND_NAME = "PostgreSQL (repositories/postgres)"

__all__ = [
    "PostgresRubricRepository",
    "POSTGRES_BACKEND_NAME",
    "PostgresAuditLogRepository",
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
```

- [ ] **Step 4: Wire the container**

In `core/container.py`, add the import:

```python
from repositories.interfaces import (
    RubricRepository,
    ApplicationRepository,
    AuditLogRepository,
    BugReportRepository,
    CandidateRepository,
    EvaluationRepository,
    JobRepository,
    SessionRepository,
    TranscriptRepository,
    UserRepository,
)
```

Add a field to `ServiceContainer` (after `bug_report_repository`, before `auth_provider`):

```python
    # Admin console: impersonation accountability trail (Task 2/3).
    audit_log_repository: AuditLogRepository = None
```

Note this field needs a value on EVERY construction path, so mark it non-defaulted instead — since dataclass fields without defaults must precede defaulted ones, place it directly after `bug_report_repository: BugReportRepository` (no default) rather than giving it `= None`:

```python
    bug_report_repository: BugReportRepository
    # Admin console: impersonation accountability trail (Task 2/3).
    audit_log_repository: AuditLogRepository
    auth_provider: AuthProvider = field(default_factory=AnonymousAuthProvider)
```

Add `"audit_log_repository"` to the `aclose()` loop's tuple (after `"bug_report_repository"`):

```python
        for name in (
            "session_repository",
            "transcript_repository",
            "job_repository",
            "candidate_repository",
            "application_repository",
            "rubric_repository",
            "evaluation_repository",
            "evaluation_dispatcher",
            "bug_report_repository",
            "audit_log_repository",
            "auth_provider",
            "database_pool",
        ):
```

In `build_default_container`, Postgres branch: add the import and pass the argument:

```python
    if settings.database_url:
        from repositories.postgres import (
            POSTGRES_BACKEND_NAME,
            PostgresApplicationRepository,
            PostgresAuditLogRepository,
            PostgresRubricRepository,
            PostgresBugReportRepository,
            PostgresCandidateRepository,
            PostgresConnectionPool,
            PostgresEvaluationRepository,
            PostgresJobRepository,
            PostgresSessionRepository,
            PostgresTranscriptRepository,
            PostgresUserRepository,
        )

        logger.info("persistence backend: %s", POSTGRES_BACKEND_NAME)
        pool = PostgresConnectionPool(settings.database_url)
        user_repo = PostgresUserRepository(pool)

        auth_service = AuthService(user_repository=user_repo, settings=settings)
        auth_provider = JWTAuthProvider(auth_service)

        return ServiceContainer(
            settings=settings,
            session_repository=PostgresSessionRepository(pool),
            transcript_repository=PostgresTranscriptRepository(pool),
            job_repository=PostgresJobRepository(pool),
            candidate_repository=PostgresCandidateRepository(pool),
            application_repository=PostgresApplicationRepository(pool),
            rubric_repository=PostgresRubricRepository(pool),
            evaluation_repository=PostgresEvaluationRepository(pool),
            user_repository=user_repo,
            bug_report_repository=PostgresBugReportRepository(pool),
            audit_log_repository=PostgresAuditLogRepository(pool),
            evaluation_dispatcher=AsyncTaskEvaluationDispatcher(),
            auth_provider=auth_provider,
            persistence_is_ephemeral=False,
            database_pool=pool,
        )
```

And the in-memory branch:

```python
    from repositories.memory import (
        EPHEMERAL_BACKEND_NAME,
        InMemoryApplicationRepository,
        InMemoryAuditLogRepository,
        InMemoryRubricRepository,
        InMemoryBugReportRepository,
        InMemoryCandidateRepository,
        InMemoryEvaluationRepository,
        InMemoryJobRepository,
        InMemorySessionRepository,
        InMemoryTranscriptRepository,
        InMemoryUserRepository,
    )

    logger.warning(
        "persistence is EPHEMERAL: using %s. Interview session records, "
        "sealed transcripts, jobs, candidates, applications and evaluation "
        "jobs will not survive a restart. Set DATABASE_URL to use "
        "repositories/postgres instead.",
        EPHEMERAL_BACKEND_NAME,
    )

    return ServiceContainer(
        settings=settings,
        session_repository=InMemorySessionRepository(),
        transcript_repository=InMemoryTranscriptRepository(),
        job_repository=InMemoryJobRepository(),
        candidate_repository=InMemoryCandidateRepository(),
        application_repository=InMemoryApplicationRepository(),
        rubric_repository=InMemoryRubricRepository(),
        evaluation_repository=InMemoryEvaluationRepository(),
        user_repository=InMemoryUserRepository(),
        bug_report_repository=InMemoryBugReportRepository(),
        audit_log_repository=InMemoryAuditLogRepository(),
        evaluation_dispatcher=AsyncTaskEvaluationDispatcher(),
        auth_provider=AnonymousAuthProvider(),
        persistence_is_ephemeral=True,
    )
```

- [ ] **Step 5: Add the dependency provider**

In `core/dependencies.py`, add the import (`AuditLogRepository` to the existing `from repositories.interfaces import (...)` block) and the function, near `get_user_repository`:

```python
def get_audit_log_repository(request: Request) -> AuditLogRepository:
    return _container_from_app(request.app).audit_log_repository
```

Add `"get_audit_log_repository"` and `"AuditLogRepository"` (import only, no `__all__` entry needed for the type) to the module's `__all__` list.

- [ ] **Step 6: Run the full non-Postgres suite to confirm nothing broke**

Run: `pytest tests/ -v -k "not postgres"`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add repositories/postgres/schema.sql repositories/postgres/audit_log_repository.py \
        repositories/postgres/__init__.py core/container.py core/dependencies.py
git commit -m "feat(admin): add PostgresAuditLogRepository and wire it into the container"
```

---

## Task 4: `CandidateRecord.is_hidden` and `CandidateRepository` moderation support

**Files:**
- Modify: `repositories/interfaces.py`
- Modify: `repositories/memory.py`
- Modify: `repositories/postgres/schema.sql`
- Modify: `repositories/postgres/repository.py`
- Modify: `services/candidate_service.py`
- Test: `tests/test_backend_admin.py`

**Interfaces:**
- Produces: `CandidateRecord.is_hidden: bool = False`; `CandidateRepository.list_candidates(*, include_hidden: bool = False)`; `CandidateRepository.set_hidden(candidate_id: str, is_hidden: bool) -> Optional[CandidateRecord]`. Used by Task 13 (`AdminService`) and by `CandidateService.list_candidates` (Task 4 Step 6) / `api/routes/candidates.py`'s existing `GET /candidates`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backend_admin.py`:

```python
# ---------------------------------------------------------------------------
# CandidateRecord.is_hidden / CandidateRepository moderation
# ---------------------------------------------------------------------------

from repositories.interfaces import CandidateRecord
from repositories.memory import InMemoryCandidateRepository
from schemas.resume import ParsedResume


def _bare_resume(candidate_id: str) -> ParsedResume:
    return ParsedResume(candidate_id=candidate_id, candidate_name="Jane Doe")


class TestCandidateModeration:
    def test_is_hidden_defaults_to_false(self):
        record = CandidateRecord(
            candidate_id="cand_1", user_id="user_1", resume=_bare_resume("cand_1"),
        )
        assert record.is_hidden is False

    @pytest.mark.asyncio
    async def test_list_candidates_excludes_hidden_by_default(self):
        repo = InMemoryCandidateRepository()
        await repo.save(CandidateRecord(
            candidate_id="cand_1", user_id="user_1", resume=_bare_resume("cand_1"),
        ))
        await repo.save(CandidateRecord(
            candidate_id="cand_2", user_id="user_2", resume=_bare_resume("cand_2"),
            is_hidden=True,
        ))
        visible = await repo.list_candidates()
        assert [c.candidate_id for c in visible] == ["cand_1"]

        everyone = await repo.list_candidates(include_hidden=True)
        assert {c.candidate_id for c in everyone} == {"cand_1", "cand_2"}

    @pytest.mark.asyncio
    async def test_set_hidden_toggles_and_is_reversible(self):
        repo = InMemoryCandidateRepository()
        await repo.save(CandidateRecord(
            candidate_id="cand_1", user_id="user_1", resume=_bare_resume("cand_1"),
        ))
        hidden = await repo.set_hidden("cand_1", True)
        assert hidden.is_hidden is True
        restored = await repo.set_hidden("cand_1", False)
        assert restored.is_hidden is False

    @pytest.mark.asyncio
    async def test_set_hidden_unknown_candidate_returns_none(self):
        repo = InMemoryCandidateRepository()
        assert await repo.set_hidden("does_not_exist", True) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_admin.py -v`
Expected: FAIL — `TypeError: list_candidates() got an unexpected keyword argument 'include_hidden'` (or `is_hidden` rejected on `CandidateRecord`).

- [ ] **Step 3: Add `is_hidden` to `CandidateRecord`**

In `repositories/interfaces.py`, extend `CandidateRecord` (currently ending at line 402):

```python
    candidate_id: str
    user_id: str
    resume: ParsedResume
    used_fallback: bool = False
    parse_warning: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None
    # Admin console moderation flag (spec §3.2). False means visible in
    # every normal, non-admin listing - see CandidateRepository.list_candidates.
    is_hidden: bool = False
```

- [ ] **Step 4: Extend `CandidateRepository`'s ABC**

Update `list_candidates` and add `set_hidden`:

```python
    @abstractmethod
    async def list_candidates(self, *, include_hidden: bool = False) -> list[CandidateRecord]:
        """All candidates, newest first. Admin-hidden candidates are excluded
        unless asked for, matching JobRepository.list_jobs' `include_archived`
        pattern."""

    @abstractmethod
    async def set_hidden(self, candidate_id: str, is_hidden: bool) -> Optional[CandidateRecord]:
        """Set the admin moderation flag. Returns the updated record, or
        None if the candidate does not exist. Idempotent and reversible -
        setting the same value twice is not an error."""
```

- [ ] **Step 5: Implement in `repositories/memory.py`**

Update `InMemoryCandidateRepository.list_candidates` and add `set_hidden`:

```python
    async def list_candidates(self, *, include_hidden: bool = False) -> List[CandidateRecord]:
        async with self._lock:
            matches = [
                c for c in self._candidates.values() if include_hidden or not c.is_hidden
            ]
        return sorted(matches, key=lambda r: r.created_at, reverse=True)

    async def set_hidden(self, candidate_id: str, is_hidden: bool) -> Optional[CandidateRecord]:
        async with self._lock:
            existing = self._candidates.get(candidate_id)
            if existing is None:
                return None
            updated = existing.model_copy(
                update={"is_hidden": is_hidden, "updated_at": datetime.now(timezone.utc)}
            )
            self._candidates[candidate_id] = updated
            return updated
```

(This replaces the existing `list_candidates` method in `InMemoryCandidateRepository`; `set_hidden` is new, placed directly after it.)

- [ ] **Step 6: Update `services/candidate_service.py`'s passthrough**

```python
    async def list_candidates(self, *, include_hidden: bool = False) -> list[CandidateRecord]:
        return await self._candidates.list_candidates(include_hidden=include_hidden)
```

`api/routes/candidates.py`'s existing `GET /candidates` route needs no code change: it calls `service.list_candidates()` with no arguments, so it keeps excluding hidden candidates by default — exactly the spec §5.2's "existing candidate/recruiter-facing listing endpoints ... exclude hidden ... records by default" requirement for this endpoint.

- [ ] **Step 7: Add the Postgres column and implementation**

In `repositories/postgres/schema.sql`, add to the `candidates` table definition (after `updated_at timestamptz` in that table):

```sql
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id    text PRIMARY KEY,
    user_id         text NOT NULL REFERENCES users(user_id),
    resume          jsonb NOT NULL,
    used_fallback   boolean NOT NULL DEFAULT false,
    parse_warning   text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz,
    -- Admin console moderation flag (2026-09-06). Nullable-safe: DEFAULT
    -- false, so existing rows need no backfill.
    is_hidden       boolean NOT NULL DEFAULT false
);

-- Add candidates.is_hidden for databases created before the admin console
-- existed - same guarded, re-runnable shape as users.full_name etc. above.
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS is_hidden boolean NOT NULL DEFAULT false;
```

Backs the admin moderation view and the default-hides-hidden listing:

```sql
CREATE INDEX IF NOT EXISTS idx_candidates_is_hidden ON candidates (is_hidden);
```

In `repositories/postgres/repository.py`, update `_candidate_record_from_row`:

```python
def _candidate_record_from_row(row: asyncpg.Record) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=row["candidate_id"],
        user_id=row["user_id"],
        resume=ParsedResume.model_validate(row["resume"]),
        used_fallback=row["used_fallback"],
        parse_warning=row["parse_warning"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        is_hidden=row["is_hidden"],
    )
```

Update `PostgresCandidateRepository.list_candidates` and add `set_hidden`:

```python
    async def list_candidates(self, *, include_hidden: bool = False) -> List[CandidateRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM candidates
                WHERE ($1 OR NOT is_hidden)
                ORDER BY created_at DESC
                """,
                include_hidden,
            )
        return [_candidate_record_from_row(row) for row in rows]

    async def set_hidden(self, candidate_id: str, is_hidden: bool) -> Optional[CandidateRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE candidates
                SET is_hidden = $2, updated_at = now()
                WHERE candidate_id = $1
                RETURNING *
                """,
                candidate_id,
                is_hidden,
            )
        return _candidate_record_from_row(row) if row is not None else None
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `pytest tests/test_backend_admin.py -v`
Expected: PASS

Run: `pytest tests/ -v -k "not postgres"`
Expected: PASS (regression check — `ParsedResume`'s exact required fields may differ from the test's `_bare_resume` helper; if `ParsedResume` requires additional fields, extend the helper to match `schemas/resume.py`'s actual model before proceeding, rather than loosening the test's intent)

- [ ] **Step 9: Commit**

```bash
git add repositories/interfaces.py repositories/memory.py repositories/postgres/schema.sql \
        repositories/postgres/repository.py services/candidate_service.py tests/test_backend_admin.py
git commit -m "feat(admin): add CandidateRecord.is_hidden and CandidateRepository moderation support"
```

---

## Task 5: `Application.is_hidden` and `ApplicationRepository` moderation support

**Files:**
- Modify: `schemas/application.py`
- Modify: `repositories/interfaces.py`
- Modify: `repositories/memory.py`
- Modify: `repositories/postgres/schema.sql`
- Modify: `repositories/postgres/repository.py`
- Test: `tests/test_backend_admin.py`

**Interfaces:**
- Produces: `Application.is_hidden: bool = False`; `ApplicationRepository.list_for_job(job_id, *, include_hidden=False)`, `.list_for_candidate(candidate_id, *, include_hidden=False)`, `.list_all(*, include_hidden=False) -> list[Application]` (new — no "every application" method exists today), `.set_hidden(application_id, is_hidden) -> Optional[Application]`. Used by Task 14 (`AdminService`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backend_admin.py`:

```python
# ---------------------------------------------------------------------------
# Application.is_hidden / ApplicationRepository moderation
# ---------------------------------------------------------------------------

from schemas.application import Application
from repositories.memory import InMemoryApplicationRepository


class TestApplicationModeration:
    def test_is_hidden_defaults_to_false(self):
        app = Application(application_id="app_1", job_id="job_1", candidate_id="cand_1")
        assert app.is_hidden is False

    @pytest.mark.asyncio
    async def test_list_all_excludes_hidden_by_default(self):
        repo = InMemoryApplicationRepository()
        await repo.save(Application(application_id="app_1", job_id="job_1", candidate_id="cand_1"))
        await repo.save(Application(
            application_id="app_2", job_id="job_1", candidate_id="cand_2", is_hidden=True,
        ))
        visible = await repo.list_all()
        assert [a.application_id for a in visible] == ["app_1"]
        everyone = await repo.list_all(include_hidden=True)
        assert {a.application_id for a in everyone} == {"app_1", "app_2"}

    @pytest.mark.asyncio
    async def test_list_for_job_excludes_hidden_by_default(self):
        repo = InMemoryApplicationRepository()
        await repo.save(Application(application_id="app_1", job_id="job_1", candidate_id="cand_1"))
        await repo.save(Application(
            application_id="app_2", job_id="job_1", candidate_id="cand_2", is_hidden=True,
        ))
        visible = await repo.list_for_job("job_1")
        assert [a.application_id for a in visible] == ["app_1"]
        everyone = await repo.list_for_job("job_1", include_hidden=True)
        assert len(everyone) == 2

    @pytest.mark.asyncio
    async def test_set_hidden_toggles_and_is_reversible(self):
        repo = InMemoryApplicationRepository()
        await repo.save(Application(application_id="app_1", job_id="job_1", candidate_id="cand_1"))
        hidden = await repo.set_hidden("app_1", True)
        assert hidden.is_hidden is True
        restored = await repo.set_hidden("app_1", False)
        assert restored.is_hidden is False

    @pytest.mark.asyncio
    async def test_set_hidden_unknown_application_returns_none(self):
        repo = InMemoryApplicationRepository()
        assert await repo.set_hidden("does_not_exist", True) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_admin.py -v`
Expected: FAIL — `AttributeError: 'InMemoryApplicationRepository' object has no attribute 'list_all'`

- [ ] **Step 3: Add `is_hidden` to `Application`**

In `schemas/application.py`, extend the model (after `session_id`, before `created_at`):

```python
    session_id: Optional[str] = None

    # Admin console moderation flag (spec §3.2). False means visible in
    # every normal, non-admin listing.
    is_hidden: bool = False

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

- [ ] **Step 4: Extend `ApplicationRepository`'s ABC**

In `repositories/interfaces.py`, replace `list_for_job`/`list_for_candidate` and add `list_all`/`set_hidden`:

```python
    @abstractmethod
    async def list_for_job(
        self, job_id: str, *, include_hidden: bool = False
    ) -> list[Application]:
        """Every application against one job - the pool
        `MatchingService`/`ApplicationService` rank and shortlist from.
        Admin-hidden applications are excluded unless asked for."""

    @abstractmethod
    async def list_for_candidate(
        self, candidate_id: str, *, include_hidden: bool = False
    ) -> list[Application]:
        """Every application belonging to one candidate. Scoped, for the
        same isolation reason `SessionRepository.list_for_candidate` is.
        Admin-hidden applications are excluded unless asked for."""

    @abstractmethod
    async def list_all(self, *, include_hidden: bool = False) -> list[Application]:
        """Every application on the platform, newest first - the admin
        moderation view (spec §5.2). No candidate/recruiter-facing route
        calls this; it exists solely for `AdminService`."""

    @abstractmethod
    async def set_hidden(self, application_id: str, is_hidden: bool) -> Optional[Application]:
        """Set the admin moderation flag. Returns the updated application,
        or None if it does not exist. Idempotent and reversible."""
```

- [ ] **Step 5: Implement in `repositories/memory.py`**

Replace `InMemoryApplicationRepository.list_for_job`/`list_for_candidate` and add `list_all`/`set_hidden`:

```python
    async def list_for_job(
        self, job_id: str, *, include_hidden: bool = False
    ) -> List[Application]:
        async with self._lock:
            matches = [
                a for a in self._applications.values()
                if a.job_id == job_id and (include_hidden or not a.is_hidden)
            ]
        return sorted(matches, key=lambda a: a.created_at, reverse=True)

    async def list_for_candidate(
        self, candidate_id: str, *, include_hidden: bool = False
    ) -> List[Application]:
        async with self._lock:
            matches = [
                a for a in self._applications.values()
                if a.candidate_id == candidate_id and (include_hidden or not a.is_hidden)
            ]
        return sorted(matches, key=lambda a: a.created_at, reverse=True)

    async def list_all(self, *, include_hidden: bool = False) -> List[Application]:
        async with self._lock:
            matches = [
                a for a in self._applications.values() if include_hidden or not a.is_hidden
            ]
        return sorted(matches, key=lambda a: a.created_at, reverse=True)

    async def set_hidden(self, application_id: str, is_hidden: bool) -> Optional[Application]:
        async with self._lock:
            existing = self._applications.get(application_id)
            if existing is None:
                return None
            updated = existing.model_copy(
                update={"is_hidden": is_hidden, "updated_at": datetime.now(timezone.utc)}
            )
            self._applications[application_id] = updated
            return updated
```

- [ ] **Step 6: Add the Postgres column and implementation**

In `repositories/postgres/schema.sql`, add to the `applications` table (after `updated_at timestamptz,`, before the `CONSTRAINT uq_applications_job_candidate` line):

```sql
    -- Admin console moderation flag (2026-09-06).
    is_hidden       boolean NOT NULL DEFAULT false,
```

And after the table's existing indexes:

```sql
-- Add applications.is_hidden for databases created before the admin
-- console existed.
ALTER TABLE applications ADD COLUMN IF NOT EXISTS is_hidden boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_applications_is_hidden ON applications (is_hidden);
```

In `repositories/postgres/repository.py`, update `_application_from_row`:

```python
def _application_from_row(row: asyncpg.Record) -> Application:
    return Application(
        application_id=row["application_id"],
        job_id=row["job_id"],
        candidate_id=row["candidate_id"],
        status=ApplicationStatus(row["status"]),
        matching_score=(
            MatchingScore.model_validate(row["matching_score"])
            if row["matching_score"] is not None
            else None
        ),
        semantic_score=row["semantic_score"],
        session_id=row["session_id"],
        is_hidden=row["is_hidden"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
```

Update `PostgresApplicationRepository.save`'s INSERT to include `is_hidden` (add it to the column list, the `VALUES` placeholders, the `ON CONFLICT ... SET` clause, the `RETURNING` list, and the positional argument — following the exact pattern already used for `semantic_score` two columns above it), then replace `list_for_job`/`list_for_candidate` and add `list_all`/`set_hidden`:

```python
    async def list_for_job(
        self, job_id: str, *, include_hidden: bool = False
    ) -> List[Application]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM applications
                WHERE job_id = $1 AND ($2 OR NOT is_hidden)
                ORDER BY created_at DESC
                """,
                job_id,
                include_hidden,
            )
        return [_application_from_row(row) for row in rows]

    async def list_for_candidate(
        self, candidate_id: str, *, include_hidden: bool = False
    ) -> List[Application]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM applications
                WHERE candidate_id = $1 AND ($2 OR NOT is_hidden)
                ORDER BY created_at DESC
                """,
                candidate_id,
                include_hidden,
            )
        return [_application_from_row(row) for row in rows]

    async def list_all(self, *, include_hidden: bool = False) -> List[Application]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM applications
                WHERE ($1 OR NOT is_hidden)
                ORDER BY created_at DESC
                """,
                include_hidden,
            )
        return [_application_from_row(row) for row in rows]

    async def set_hidden(self, application_id: str, is_hidden: bool) -> Optional[Application]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE applications
                SET is_hidden = $2, updated_at = now()
                WHERE application_id = $1
                RETURNING *
                """,
                application_id,
                is_hidden,
            )
        return _application_from_row(row) if row is not None else None
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_backend_admin.py -v`
Expected: PASS

Run: `pytest tests/ -v -k "not postgres"`
Expected: PASS — every existing caller of `list_for_job`/`list_for_candidate` (`services/application_service.py`, `services/recruiter_service.py`) calls them with no `include_hidden` argument, so they keep their current behavior (excluding nothing that wasn't already excluded, since `is_hidden` defaults to `False` on every existing `Application`).

- [ ] **Step 8: Commit**

```bash
git add schemas/application.py repositories/interfaces.py repositories/memory.py \
        repositories/postgres/schema.sql repositories/postgres/repository.py tests/test_backend_admin.py
git commit -m "feat(admin): add Application.is_hidden and ApplicationRepository moderation support"
```

---

## Task 6: `JobRepository.restore`

**Files:**
- Modify: `repositories/interfaces.py`
- Modify: `repositories/memory.py`
- Modify: `repositories/postgres/repository.py`
- Test: `tests/test_backend_admin.py`

**Interfaces:**
- Produces: `JobRepository.restore(job_id: str) -> Optional[JobRecord]` — sets `is_active=True`, the un-hide half of the existing `archive` method (spec §5.2). Used by Task 12's `AdminService.set_job_active`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backend_admin.py`:

```python
# ---------------------------------------------------------------------------
# JobRepository.restore
# ---------------------------------------------------------------------------

from repositories.memory import InMemoryJobRepository
from repositories.interfaces import JobRecord
from schemas.job import JobDescription


def _bare_job(job_id: str) -> JobDescription:
    return JobDescription(job_id=job_id, title="Engineer", description="Build things.")


class TestJobRestore:
    @pytest.mark.asyncio
    async def test_restore_reactivates_an_archived_job(self):
        repo = InMemoryJobRepository()
        await repo.save(JobRecord(job_id="job_1", job=_bare_job("job_1")))
        await repo.archive("job_1")
        restored = await repo.restore("job_1")
        assert restored.is_active is True

    @pytest.mark.asyncio
    async def test_restore_unknown_job_returns_none(self):
        repo = InMemoryJobRepository()
        assert await repo.restore("does_not_exist") is None

    @pytest.mark.asyncio
    async def test_restore_is_idempotent(self):
        repo = InMemoryJobRepository()
        await repo.save(JobRecord(job_id="job_1", job=_bare_job("job_1")))
        first = await repo.restore("job_1")
        second = await repo.restore("job_1")
        assert first.is_active is True
        assert second.is_active is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_admin.py -v`
Expected: FAIL — `AttributeError: 'InMemoryJobRepository' object has no attribute 'restore'` (adjust `_bare_job`'s fields to match `schemas/job.py:JobDescription`'s actual required fields if this fails for a different reason — `JobDescription` may require more than `job_id`/`title`/`description`; check the schema before assuming the test itself is correct)

- [ ] **Step 3: Extend `JobRepository`'s ABC**

In `repositories/interfaces.py`, add after `archive`:

```python
    @abstractmethod
    async def restore(self, job_id: str) -> Optional[JobRecord]:
        """Set `is_active=True` - the reverse of `archive`. Returns the
        updated record, or None if the job does not exist. Idempotent:
        restoring an already-active job is not an error. Used by the admin
        console to un-hide a job an admin previously hid (spec §5.2) - a
        recruiter's own re-post flow, if one exists, is a separate concern
        and not required to route through this method."""
```

- [ ] **Step 4: Implement in `repositories/memory.py`**

Add to `InMemoryJobRepository`, after `archive`:

```python
    async def restore(self, job_id: str) -> Optional[JobRecord]:
        async with self._lock:
            existing = self._jobs.get(job_id)
            if existing is None:
                return None
            restored = existing.model_copy(
                update={"is_active": True, "updated_at": datetime.now(timezone.utc)}
            )
            self._jobs[job_id] = restored
            return restored
```

- [ ] **Step 5: Implement in `repositories/postgres/repository.py`**

Add to `PostgresJobRepository`, after `archive`:

```python
    async def restore(self, job_id: str) -> Optional[JobRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE jobs
                SET is_active = true, updated_at = now()
                WHERE job_id = $1
                RETURNING job_id, job, is_active, created_at, updated_at
                """,
                job_id,
            )
        return _job_record_from_row(row) if row is not None else None
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_backend_admin.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add repositories/interfaces.py repositories/memory.py repositories/postgres/repository.py \
        tests/test_backend_admin.py
git commit -m "feat(admin): add JobRepository.restore"
```

---

## Task 7: `require_admin` dependency

**Files:**
- Modify: `core/security.py`
- Test: `tests/test_backend_admin.py`

**Interfaces:**
- Consumes: `require_authenticated`, `Principal`, `get_settings` (existing, `core/security.py`); `UserRepository.get_by_id` (existing); `core.dependencies.get_user_repository` (existing).
- Produces: `require_admin(principal, settings, user_repository) -> Principal` — a FastAPI dependency, `Depends(require_admin)`, consumed by every route in Task 10/12/13/14/16/20's `api/routes/admin.py`.

`require_admin` lives in `core/security.py` next to `require_authenticated`/`require_scopes`, but it needs a `UserRepository` — nothing else in that file has a repository dependency, so it takes one as a FastAPI-injected parameter (via `core.dependencies.get_user_repository`) exactly the way a route handler would, rather than reaching into `request.app.state` directly.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backend_admin.py`:

```python
# ---------------------------------------------------------------------------
# require_admin
# ---------------------------------------------------------------------------

from fastapi import Depends, FastAPI
from core.security import require_admin, JWTAuthProvider
from core.container import ServiceContainer
from repositories.memory import (
    InMemoryApplicationRepository,
    InMemoryAuditLogRepository,
    InMemoryBugReportRepository,
    InMemoryCandidateRepository,
    InMemoryEvaluationRepository,
    InMemoryJobRepository,
    InMemoryRubricRepository,
    InMemorySessionRepository,
    InMemoryTranscriptRepository,
    InMemoryUserRepository,
)
from services.evaluation_dispatcher import AsyncTaskEvaluationDispatcher
from repositories.interfaces import UserRecord


def _admin_app():
    """A minimal FastAPI app with one require_admin-gated route, wired the
    same way _authenticated_app() is in tests/test_backend_profile.py."""
    settings = AppSettings(auth_enabled=True)
    user_repo = InMemoryUserRepository()
    auth_service = AuthService(user_repository=user_repo, settings=settings)
    container = ServiceContainer(
        settings=settings,
        session_repository=InMemorySessionRepository(),
        transcript_repository=InMemoryTranscriptRepository(),
        job_repository=InMemoryJobRepository(),
        candidate_repository=InMemoryCandidateRepository(),
        application_repository=InMemoryApplicationRepository(),
        rubric_repository=InMemoryRubricRepository(),
        evaluation_repository=InMemoryEvaluationRepository(),
        user_repository=user_repo,
        bug_report_repository=InMemoryBugReportRepository(),
        audit_log_repository=InMemoryAuditLogRepository(),
        evaluation_dispatcher=AsyncTaskEvaluationDispatcher(),
        auth_provider=JWTAuthProvider(auth_service),
        persistence_is_ephemeral=True,
    )
    app = FastAPI()
    app.state.container = container

    @app.get("/admin-only")
    async def admin_only(principal=Depends(require_admin)):
        return {"subject_id": principal.subject_id}

    return app, auth_service, user_repo


class TestRequireAdmin:
    def test_rejects_unauthenticated_caller(self):
        app, _auth_service, _user_repo = _admin_app()
        client = TestClient(app)
        response = client.get("/admin-only")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_rejects_a_non_admin_caller(self):
        app, auth_service, _user_repo = _admin_app()
        client = TestClient(app)
        _user, access_token, _refresh = await auth_service.signup(
            email="candidate@example.com", password="password123", user_type="candidate",
        )
        response = client.get("/admin-only", headers={"Authorization": f"Bearer {access_token}"})
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_allows_an_admin_caller(self):
        app, auth_service, user_repo = _admin_app()
        client = TestClient(app)
        user, access_token, _refresh = await auth_service.signup(
            email="recruiter@example.com", password="password123", user_type="recruiter",
        )
        # Promote out-of-band, mirroring scripts/promote_admin.py (Task 8).
        await user_repo.save(user.model_copy(update={"user_type": "admin"}))
        response = client.get("/admin-only", headers={"Authorization": f"Bearer {access_token}"})
        assert response.status_code == 200
        assert response.json()["subject_id"] == user.user_id

    @pytest.mark.asyncio
    async def test_a_demoted_admin_loses_access_immediately(self):
        """The JWT still claims user_type=admin (tokens are not re-issued on
        a role change) - require_admin must re-check the CURRENT UserRecord,
        not the token's stale claim."""
        app, auth_service, user_repo = _admin_app()
        client = TestClient(app)
        user, _access, _refresh = await auth_service.signup(
            email="admin@example.com", password="password123", user_type="recruiter",
        )
        await user_repo.save(user.model_copy(update={"user_type": "admin"}))
        _admin_user, admin_token, _r = await auth_service.login(user.email, "password123")

        response = client.get("/admin-only", headers={"Authorization": f"Bearer {admin_token}"})
        assert response.status_code == 200

        await user_repo.save(user.model_copy(update={"user_type": "recruiter"}))
        response_after_demotion = client.get(
            "/admin-only", headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response_after_demotion.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_backend_admin.py -v`
Expected: FAIL — `ImportError: cannot import name 'require_admin' from 'core.security'`

- [ ] **Step 3: Add `require_admin`**

In `core/security.py`, add after `require_scopes` (before `__all__`):

```python
async def require_admin(
    principal: Principal = Depends(require_authenticated),
    settings: AppSettings = Depends(get_settings),
    user_repository=Depends(_get_user_repository_for_security),
) -> Principal:
    """Endpoint dependency: reject any caller whose CURRENT account is not
    an admin.

    Mirrors `require_authenticated` exactly (inert while `AUTH_ENABLED=false`,
    same 401-for-anonymous behaviour via the chained `Depends`), plus one
    addition: it re-reads `UserRecord.user_type` from the repository on every
    call rather than trusting the JWT's `user_type` claim. A role change or
    deactivation takes effect on an admin's very next request instead of
    waiting for their access token to expire - the same freshness guarantee
    `AuthService.login`'s `is_active` check already gives login itself.

    403, not 404: the route existing is not a secret (spec §5).
    """
    if not settings.auth_enabled:
        return principal
    if not principal.is_authenticated:
        raise UnauthorizedError()
    user = await user_repository.get_by_id(principal.subject_id)
    if user is None or user.user_type != "admin" or not user.is_active:
        raise ForbiddenError(
            "This action requires an administrator account.",
            internal_detail=(
                f"principal subject_id={principal.subject_id!r} is not an active admin"
            ),
        )
    return principal
```

Add the small resolver `_get_user_repository_for_security` just above it, so `core/security.py` does not need to import `core/dependencies.py` (which itself imports service classes — importing it here would be circular, since `core/dependencies.py` imports `AuthService`, and nothing currently imports `core/security.py` from there, but keeping the dependency direction one-way avoids introducing that risk):

```python
def _get_user_repository_for_security(request: Request):
    """The `UserRepository`, resolved the same way `get_auth_provider`
    above resolves the auth provider - reading straight from
    `app.state.container` rather than importing `core.dependencies`, so
    this module's only dependency stays on `core.container`, never on the
    service-constructing layer built on top of it."""
    container = getattr(request.app.state, "container", None)
    return getattr(container, "user_repository", None)
```

Add both names to `__all__`:

```python
__all__ = [
    "ANONYMOUS",
    "AnonymousAuthProvider",
    "AuthProvider",
    "JWTAuthProvider",
    "Principal",
    "PrincipalType",
    "get_auth_provider",
    "get_principal",
    "require_admin",
    "require_authenticated",
    "require_scopes",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_backend_admin.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `pytest tests/ -v -k "not postgres"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add core/security.py tests/test_backend_admin.py
git commit -m "feat(admin): add require_admin dependency, re-checking UserRecord.user_type on every call"
```

---

## Task 8: `scripts/promote_admin.py`

**Files:**
- Create: `scripts/promote_admin.py`

**Interfaces:**
- Consumes: `AppSettings.from_env()` (existing, `core/config.py`), `core.container.build_default_container`, `UserRepository.get_by_email`/`save` (existing).
- Produces: a standalone operator script — nothing else in this codebase imports it (spec §4: "a deploy-time operator action, not an application feature").

This is the only way the FIRST admin gets created (spec §4) — everything after this bootstraps through `PATCH /admin/users/{id}` (Task 10).

- [ ] **Step 1: Write the script**

```python
"""
Operator script: promote an existing user to `user_type == 'admin'`.

Run manually against the target database - this is a deploy-time operator
action, not an application feature (design spec §4). There is no
self-service path to becoming an admin anywhere in this codebase; this
script and `PATCH /admin/users/{id}` (once at least one admin exists) are
the only two ways `user_type` ever becomes 'admin'.

Usage:
    python -m scripts.promote_admin someone@example.com

Reads DATABASE_URL (and any other AppSettings env vars) from the
environment exactly like the running service does - see
core/config.py:AppSettings.from_env. Against an empty/unset DATABASE_URL
this promotes a user in the ephemeral in-memory store, which is only useful
for local testing of this script itself; a real promotion always needs
DATABASE_URL pointed at the deployed database.
"""
from __future__ import annotations

import asyncio
import sys

from core.config import AppSettings
from core.container import build_default_container


async def promote(email: str) -> None:
    settings = AppSettings.from_env()
    container = build_default_container(settings)
    try:
        user = await container.user_repository.get_by_email(email)
        if user is None:
            print(f"No account found for email {email!r}.", file=sys.stderr)
            sys.exit(1)
        if user.user_type == "admin":
            print(f"{email} is already an admin. Nothing to do.")
            return
        updated = user.model_copy(update={"user_type": "admin"})
        await container.user_repository.save(updated)
        print(f"Promoted {email} (user_id={user.user_id}) to admin.")
    finally:
        await container.aclose()


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.promote_admin <email>", file=sys.stderr)
        sys.exit(2)
    asyncio.run(promote(sys.argv[1]))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Manual verification against the in-memory backend**

Run: `DATABASE_URL= python -m scripts.promote_admin nobody@example.com`
Expected: prints `No account found for email 'nobody@example.com'.` and exits with status 1 (proves the script runs and resolves a container without crashing; a real promotion needs a real `DATABASE_URL` and an already-signed-up account, neither of which exist in this throwaway run).

- [ ] **Step 3: Commit**

```bash
git add scripts/promote_admin.py
git commit -m "feat(admin): add scripts/promote_admin.py, the out-of-band first-admin bootstrap"
```

---

## Task 9: `UserRepository.list_users`

**Files:**
- Modify: `repositories/interfaces.py`
- Modify: `repositories/memory.py`
- Modify: `repositories/postgres/user_repository.py`
- Test: `tests/test_backend_admin.py`

**Interfaces:**
- Produces: `UserRepository.list_users(*, query: Optional[str] = None, user_type: Optional[str] = None, is_active: Optional[bool] = None) -> list[UserRecord]`. Used by Task 11's `AdminService.list_users` and Task 16's `AdminService.get_metrics`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backend_admin.py`:

```python
# ---------------------------------------------------------------------------
# UserRepository.list_users
# ---------------------------------------------------------------------------

class TestListUsers:
    @pytest.mark.asyncio
    async def test_lists_every_user_with_no_filters(self):
        repo = InMemoryUserRepository()
        await repo.save(UserRecord(
            user_id="u1", email="a@example.com", password_hash="h", user_type="candidate",
        ))
        await repo.save(UserRecord(
            user_id="u2", email="b@example.com", password_hash="h", user_type="recruiter",
        ))
        users = await repo.list_users()
        assert {u.user_id for u in users} == {"u1", "u2"}

    @pytest.mark.asyncio
    async def test_filters_by_user_type(self):
        repo = InMemoryUserRepository()
        await repo.save(UserRecord(
            user_id="u1", email="a@example.com", password_hash="h", user_type="candidate",
        ))
        await repo.save(UserRecord(
            user_id="u2", email="b@example.com", password_hash="h", user_type="recruiter",
        ))
        users = await repo.list_users(user_type="recruiter")
        assert [u.user_id for u in users] == ["u2"]

    @pytest.mark.asyncio
    async def test_filters_by_is_active(self):
        repo = InMemoryUserRepository()
        await repo.save(UserRecord(
            user_id="u1", email="a@example.com", password_hash="h", user_type="candidate",
            is_active=False,
        ))
        await repo.save(UserRecord(
            user_id="u2", email="b@example.com", password_hash="h", user_type="candidate",
        ))
        inactive = await repo.list_users(is_active=False)
        assert [u.user_id for u in inactive] == ["u1"]

    @pytest.mark.asyncio
    async def test_filters_by_query_matching_email_case_insensitively(self):
        repo = InMemoryUserRepository()
        await repo.save(UserRecord(
            user_id="u1", email="jane.doe@example.com", password_hash="h", user_type="candidate",
        ))
        await repo.save(UserRecord(
            user_id="u2", email="bob@example.com", password_hash="h", user_type="candidate",
        ))
        matches = await repo.list_users(query="JANE")
        assert [u.user_id for u in matches] == ["u1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_admin.py -v`
Expected: FAIL — `AttributeError: 'InMemoryUserRepository' object has no attribute 'list_users'`

- [ ] **Step 3: Extend `UserRepository`'s ABC**

In `repositories/interfaces.py`, add to `UserRepository` (after `get_by_id`):

```python
    @abstractmethod
    async def list_users(
        self,
        *,
        query: Optional[str] = None,
        user_type: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> list[UserRecord]:
        """Every user matching the given filters, newest first. `query`
        matches case-insensitively against email (and `full_name`, when
        set). All three filters are optional and independent; omitting all
        of them returns every user. Backs the admin console's user search
        (spec §5.1) - no other caller in this codebase needs an unscoped
        user listing."""
```

- [ ] **Step 4: Implement in `repositories/memory.py`**

Add to `InMemoryUserRepository`, after `get_by_id`:

```python
    async def list_users(
        self,
        *,
        query: Optional[str] = None,
        user_type: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[UserRecord]:
        async with self._lock:
            matches = list(self._users.values())
        if user_type is not None:
            matches = [u for u in matches if u.user_type == user_type]
        if is_active is not None:
            matches = [u for u in matches if u.is_active == is_active]
        if query:
            needle = query.lower()
            matches = [
                u for u in matches
                if needle in u.email.lower() or (u.full_name and needle in u.full_name.lower())
            ]
        return sorted(matches, key=lambda u: u.created_at, reverse=True)
```

- [ ] **Step 5: Implement in `repositories/postgres/user_repository.py`**

Add to `PostgresUserRepository`, after `get_by_id`:

```python
    async def list_users(
        self,
        *,
        query: Optional[str] = None,
        user_type: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> list[UserRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM users
                WHERE ($1::text IS NULL OR user_type = $1)
                  AND ($2::boolean IS NULL OR is_active = $2)
                  AND (
                    $3::text IS NULL
                    OR email ILIKE '%' || $3 || '%'
                    OR full_name ILIKE '%' || $3 || '%'
                  )
                ORDER BY created_at DESC
                """,
                user_type,
                is_active,
                query,
            )
        return [_user_record_from_row(row) for row in rows]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_backend_admin.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add repositories/interfaces.py repositories/memory.py repositories/postgres/user_repository.py \
        tests/test_backend_admin.py
git commit -m "feat(admin): add UserRepository.list_users for the admin console's user search"
```

---

## Task 10: `schemas/admin.py` and `services/admin_service.py` — user management

**Files:**
- Create: `schemas/admin.py`
- Create: `services/admin_service.py`
- Test: `tests/test_backend_admin.py`

**Interfaces:**
- Consumes: `UserRecord`/`UserRepository.list_users` (Task 9), `UserProfileResponse` (existing, `schemas/auth.py`), `NotFoundError`/`BadRequestError` (existing, `core/errors.py`).
- Produces (used by Task 11's routes and every later phase's `AdminService` method):
  - `schemas.admin.AdminUserListResponse(users: list[UserProfileResponse], total: int)` with `.from_records(cls, records) -> AdminUserListResponse`.
  - `schemas.admin.AdminUpdateUserRequest(is_active: Optional[bool] = None, user_type: Optional[str] = None)`.
  - `AdminService.__init__(*, user_repository, job_repository, candidate_repository, application_repository, audit_log_repository, auth_service)`.
  - `AdminService.list_users(*, query=None, user_type=None, is_active=None) -> list[UserRecord]`.
  - `AdminService.update_user(user_id: str, updates: dict) -> UserRecord` — raises `NotFoundError` if no such user, `BadRequestError` if `user_type` is present and not in `{'candidate','recruiter','admin'}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backend_admin.py`:

```python
# ---------------------------------------------------------------------------
# schemas/admin.py + AdminService: user management
# ---------------------------------------------------------------------------

from schemas.admin import AdminUpdateUserRequest, AdminUserListResponse
from services.admin_service import AdminService
from repositories.memory import InMemoryJobRepository as _Jobs
from repositories.memory import InMemoryCandidateRepository as _Candidates
from repositories.memory import InMemoryApplicationRepository as _Applications


def _admin_service(user_repo=None, auth_service=None):
    settings = AppSettings()
    user_repo = user_repo or InMemoryUserRepository()
    auth_service = auth_service or AuthService(user_repository=user_repo, settings=settings)
    return AdminService(
        user_repository=user_repo,
        job_repository=_Jobs(),
        candidate_repository=_Candidates(),
        application_repository=_Applications(),
        audit_log_repository=InMemoryAuditLogRepository(),
        auth_service=auth_service,
    )


class TestAdminUserListResponse:
    def test_from_records_excludes_password_hash(self):
        record = UserRecord(
            user_id="u1", email="a@example.com", password_hash="secret", user_type="candidate",
        )
        response = AdminUserListResponse.from_records([record])
        assert response.total == 1
        dumped = response.model_dump_json()
        assert "secret" not in dumped


class TestAdminServiceListUsers:
    @pytest.mark.asyncio
    async def test_delegates_to_the_repository_with_all_filters(self):
        user_repo = InMemoryUserRepository()
        await user_repo.save(UserRecord(
            user_id="u1", email="a@example.com", password_hash="h", user_type="recruiter",
        ))
        service = _admin_service(user_repo=user_repo)
        users = await service.list_users(user_type="recruiter")
        assert [u.user_id for u in users] == ["u1"]


class TestAdminServiceUpdateUser:
    @pytest.mark.asyncio
    async def test_updates_is_active(self):
        user_repo = InMemoryUserRepository()
        auth_service = AuthService(user_repository=user_repo, settings=AppSettings())
        user, _a, _r = await auth_service.signup(
            email="c@example.com", password="password123", user_type="candidate",
        )
        service = _admin_service(user_repo=user_repo, auth_service=auth_service)
        updated = await service.update_user(user.user_id, {"is_active": False})
        assert updated.is_active is False

    @pytest.mark.asyncio
    async def test_promotes_a_user_to_admin(self):
        user_repo = InMemoryUserRepository()
        auth_service = AuthService(user_repository=user_repo, settings=AppSettings())
        user, _a, _r = await auth_service.signup(
            email="r@example.com", password="password123", user_type="recruiter",
        )
        service = _admin_service(user_repo=user_repo, auth_service=auth_service)
        updated = await service.update_user(user.user_id, {"user_type": "admin"})
        assert updated.user_type == "admin"

    @pytest.mark.asyncio
    async def test_rejects_an_invalid_user_type(self):
        user_repo = InMemoryUserRepository()
        auth_service = AuthService(user_repository=user_repo, settings=AppSettings())
        user, _a, _r = await auth_service.signup(
            email="c@example.com", password="password123", user_type="candidate",
        )
        service = _admin_service(user_repo=user_repo, auth_service=auth_service)
        with pytest.raises(BadRequestError):
            await service.update_user(user.user_id, {"user_type": "superuser"})

    @pytest.mark.asyncio
    async def test_unknown_user_is_not_found(self):
        service = _admin_service()
        with pytest.raises(NotFoundError):
            await service.update_user("does_not_exist", {"is_active": False})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_backend_admin.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'schemas.admin'`

- [ ] **Step 3: Create `schemas/admin.py`**

```python
"""
Admin console request/response schemas.

Reuses `UserProfileResponse` (schemas/auth.py) rather than inventing a
second "user view" - the admin console needs the exact same fields (no
password_hash, every profile field), just for an arbitrary user instead of
only the caller's own. `AdminUpdateUserRequest` is intentionally NOT
`UpdateProfileRequest`: it exposes `is_active`/`user_type`, which a
self-service profile update must never be able to touch (see
services/auth_service.py:_PROTECTED_ACCOUNT_FIELDS) - the two request
shapes are deliberately disjoint, not one widened to cover both roles.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from repositories.interfaces import UserRecord
from schemas.auth import UserProfileResponse


# ---------------------------------------------------------------------------
# GET /admin/users
# ---------------------------------------------------------------------------

class AdminUserListResponse(BaseModel):
    users: List[UserProfileResponse]
    total: int

    @classmethod
    def from_records(cls, records: List[UserRecord]) -> "AdminUserListResponse":
        return cls(
            users=[UserProfileResponse.from_record(r) for r in records],
            total=len(records),
        )


# ---------------------------------------------------------------------------
# PATCH /admin/users/{user_id}
# ---------------------------------------------------------------------------

class AdminUpdateUserRequest(BaseModel):
    """Every field optional so an admin can change just one. Validated
    further in AdminService.update_user (user_type must be one of
    candidate/recruiter/admin) - schema-level validation can't express
    "one of these three strings AND report 400 with a client-safe message",
    since FastAPI's own 422 for a failed `pattern=` is that "invalid enum
    value" shape, not this endpoint's `BadRequestError` contract."""

    is_active: Optional[bool] = None
    user_type: Optional[str] = None


# ---------------------------------------------------------------------------
# PATCH /admin/jobs/{job_id}
# ---------------------------------------------------------------------------

class AdminSetJobActiveRequest(BaseModel):
    is_active: bool


# ---------------------------------------------------------------------------
# PATCH /admin/candidates/{candidate_id}, PATCH /admin/applications/{application_id}
# ---------------------------------------------------------------------------

class AdminSetHiddenRequest(BaseModel):
    is_hidden: bool


# ---------------------------------------------------------------------------
# GET /admin/metrics
# ---------------------------------------------------------------------------

class UserTypeCounts(BaseModel):
    candidate: int = 0
    recruiter: int = 0
    admin: int = 0


class JobCounts(BaseModel):
    active: int = 0
    hidden: int = 0


class ApplicationCounts(BaseModel):
    total: int = 0
    hidden: int = 0


class AdminMetricsResponse(BaseModel):
    users: UserTypeCounts
    jobs: JobCounts
    applications: ApplicationCounts


# ---------------------------------------------------------------------------
# POST /admin/impersonate/{user_id}
# ---------------------------------------------------------------------------

class ImpersonateResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


__all__ = [
    "AdminUserListResponse",
    "AdminUpdateUserRequest",
    "AdminSetJobActiveRequest",
    "AdminSetHiddenRequest",
    "UserTypeCounts",
    "JobCounts",
    "ApplicationCounts",
    "AdminMetricsResponse",
    "ImpersonateResponse",
]
```

- [ ] **Step 4: Create `services/admin_service.py`**

```python
"""
Admin console use cases: user management, content moderation, platform
metrics, and support impersonation.

Aggregates the same repositories every other service already depends on
(UserRepository, JobRepository, CandidateRepository, ApplicationRepository)
plus the new AuditLogRepository - no new persistence beyond that, matching
RecruiterService's own "read-only aggregation over existing repositories"
precedent (services/recruiter_service.py).

Every admin action here is only reachable once a caller has already passed
`core.security.require_admin` - this service does not re-check who is
calling; that authorization decision is the route layer's job, exactly like
every other service in this codebase.
"""
from __future__ import annotations

import uuid
from typing import Optional

from core.errors import BadRequestError, ConflictError, NotFoundError, UnauthorizedError
from core.logging import get_logger, log_context
from repositories.interfaces import (
    AuditLogRecord,
    AuditLogRepository,
    Application,
    ApplicationRepository,
    CandidateRecord,
    CandidateRepository,
    JobRecord,
    JobRepository,
    UserRecord,
    UserRepository,
)
from services.auth_service import AuthService

logger = get_logger("services.admin")

# The complete set of values PATCH /admin/users/{id} may set user_type to.
# No 'anonymous'/'service' - those are Principal-only concepts, never a
# stored account's user_type (core/security.py:PrincipalType).
_VALID_USER_TYPES = {"candidate", "recruiter", "admin"}

# Fields PATCH /admin/users/{id} may change. Deliberately wider than
# AuthService.update_profile's own allowed set (services/auth_service.py) -
# an admin may change fields a self-service profile update cannot.
_ADMIN_UPDATABLE_USER_FIELDS = {"is_active", "user_type"}


class AdminService:
    """Use cases behind every `/admin/*` route (api/routes/admin.py)."""

    def __init__(
        self,
        *,
        user_repository: UserRepository,
        job_repository: JobRepository,
        candidate_repository: CandidateRepository,
        application_repository: ApplicationRepository,
        audit_log_repository: AuditLogRepository,
        auth_service: AuthService,
    ) -> None:
        self._users = user_repository
        self._jobs = job_repository
        self._candidates = candidate_repository
        self._applications = application_repository
        self._audit_logs = audit_log_repository
        self._auth = auth_service

    # -- User management -----------------------------------------------

    async def list_users(
        self,
        *,
        query: Optional[str] = None,
        user_type: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> list[UserRecord]:
        return await self._users.list_users(query=query, user_type=user_type, is_active=is_active)

    async def update_user(self, user_id: str, updates: dict) -> UserRecord:
        """Apply a partial update to `is_active`/`user_type`.

        Raises:
            NotFoundError: If no such user exists.
            BadRequestError: If `updates['user_type']` is present and not
                one of candidate/recruiter/admin.
        """
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise NotFoundError(
                "No account found.",
                internal_detail=f"user_id={user_id!r} not found",
            )

        safe_updates = {k: v for k, v in updates.items() if k in _ADMIN_UPDATABLE_USER_FIELDS}

        if "user_type" in safe_updates and safe_updates["user_type"] not in _VALID_USER_TYPES:
            raise BadRequestError(
                f"user_type must be one of {sorted(_VALID_USER_TYPES)}.",
                internal_detail=(
                    f"admin update for user {user_id!r} supplied invalid "
                    f"user_type={safe_updates['user_type']!r}"
                ),
            )

        updated = user.model_copy(update=safe_updates)
        stored = await self._users.save(updated)

        logger.info(
            "admin updated a user account",
            extra=log_context(event="admin_user_updated", user_id=user_id, fields=sorted(safe_updates)),
        )
        return stored


__all__ = ["AdminService"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_backend_admin.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add schemas/admin.py services/admin_service.py tests/test_backend_admin.py
git commit -m "feat(admin): add AdminService and schemas/admin.py for user management"
```

---

## Task 11: `GET/PATCH /admin/users` routes + registration

**Files:**
- Create: `api/routes/admin.py`
- Modify: `api/app.py`
- Modify: `core/dependencies.py`
- Test: `tests/test_backend_admin.py`

**Interfaces:**
- Consumes: `require_admin` (Task 7), `AdminService` (Task 10), `AdminUserListResponse`/`AdminUpdateUserRequest` (Task 10), `UserProfileResponse` (existing).
- Produces: `GET /admin/users`, `PATCH /admin/users/{user_id}` — the first two of ten routes this router will eventually carry; `core.dependencies.get_admin_service(request) -> AdminService`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backend_admin.py`. This reuses `_admin_app`-style wiring but through the REAL app (`create_app`), following `tests/test_backend_profile.py`'s `_authenticated_app()` pattern exactly, so the full `/admin` router is exercised over HTTP:

```python
# ---------------------------------------------------------------------------
# HTTP layer: GET/PATCH /admin/users
# ---------------------------------------------------------------------------

from api.app import create_app


def _authenticated_admin_app():
    """Same shape as tests/test_backend_profile.py's _authenticated_app(),
    reused here (and by every later admin HTTP test) because every /admin
    route needs the identical real-JWTAuthProvider-over-in-memory wiring."""
    settings = AppSettings(auth_enabled=True)
    user_repo = InMemoryUserRepository()
    auth_service = AuthService(user_repository=user_repo, settings=settings)
    container = ServiceContainer(
        settings=settings,
        session_repository=InMemorySessionRepository(),
        transcript_repository=InMemoryTranscriptRepository(),
        job_repository=InMemoryJobRepository(),
        candidate_repository=InMemoryCandidateRepository(),
        application_repository=InMemoryApplicationRepository(),
        rubric_repository=InMemoryRubricRepository(),
        evaluation_repository=InMemoryEvaluationRepository(),
        user_repository=user_repo,
        bug_report_repository=InMemoryBugReportRepository(),
        audit_log_repository=InMemoryAuditLogRepository(),
        evaluation_dispatcher=AsyncTaskEvaluationDispatcher(),
        auth_provider=JWTAuthProvider(auth_service),
        persistence_is_ephemeral=True,
    )
    app = create_app(settings)
    app.state.container = container
    return app, auth_service, user_repo


async def _signup_admin(auth_service, user_repo, email="admin@example.com"):
    user, _access, _refresh = await auth_service.signup(
        email=email, password="password123", user_type="recruiter",
    )
    await user_repo.save(user.model_copy(update={"user_type": "admin"}))
    _promoted, access_token, _refresh2 = await auth_service.login(email, "password123")
    return _promoted, access_token


class TestAdminUsersEndpoint:
    def test_requires_admin(self):
        app, auth_service, user_repo = _authenticated_admin_app()
        client = TestClient(app)
        signup = client.post(
            "/auth/signup",
            json={"email": "c@example.com", "password": "password123", "user_type": "candidate"},
        )
        token = signup.json()["access_token"]
        response = client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403

    def test_lists_users_for_an_admin(self):
        app, auth_service, user_repo = _authenticated_admin_app()
        client = TestClient(app)
        client.post(
            "/auth/signup",
            json={"email": "c@example.com", "password": "password123", "user_type": "candidate"},
        )

        import asyncio
        _admin_user, admin_token = asyncio.get_event_loop().run_until_complete(
            _signup_admin(auth_service, user_repo)
        )

        response = client.get("/admin/users", headers={"Authorization": f"Bearer {admin_token}"})
        assert response.status_code == 200
        body = response.json()
        emails = {u["email"] for u in body["users"]}
        assert "c@example.com" in emails
        assert "admin@example.com" in emails
        assert all("password_hash" not in u for u in body["users"])

    def test_filters_by_user_type_query_param(self):
        app, auth_service, user_repo = _authenticated_admin_app()
        client = TestClient(app)
        client.post(
            "/auth/signup",
            json={"email": "c@example.com", "password": "password123", "user_type": "candidate"},
        )
        import asyncio
        _admin_user, admin_token = asyncio.get_event_loop().run_until_complete(
            _signup_admin(auth_service, user_repo)
        )
        response = client.get(
            "/admin/users?user_type=candidate", headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        body = response.json()
        assert all(u["user_type"] == "candidate" for u in body["users"])

    def test_patch_promotes_a_user_to_admin(self):
        app, auth_service, user_repo = _authenticated_admin_app()
        client = TestClient(app)
        signup = client.post(
            "/auth/signup",
            json={"email": "r@example.com", "password": "password123", "user_type": "recruiter"},
        )
        target_user_id = signup.json()["user"]["user_id"]
        import asyncio
        _admin_user, admin_token = asyncio.get_event_loop().run_until_complete(
            _signup_admin(auth_service, user_repo)
        )
        response = client.patch(
            f"/admin/users/{target_user_id}",
            json={"user_type": "admin"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert response.status_code == 200
        assert response.json()["user_type"] == "admin"

    def test_patch_rejects_invalid_user_type(self):
        app, auth_service, user_repo = _authenticated_admin_app()
        client = TestClient(app)
        signup = client.post(
            "/auth/signup",
            json={"email": "r@example.com", "password": "password123", "user_type": "recruiter"},
        )
        target_user_id = signup.json()["user"]["user_id"]
        import asyncio
        _admin_user, admin_token = asyncio.get_event_loop().run_until_complete(
            _signup_admin(auth_service, user_repo)
        )
        response = client.patch(
            f"/admin/users/{target_user_id}",
            json={"user_type": "superuser"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert response.status_code == 400

    def test_requires_authentication(self):
        app, _auth_service, _user_repo = _authenticated_admin_app()
        client = TestClient(app)
        response = client.get("/admin/users")
        assert response.status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_backend_admin.py -v`
Expected: FAIL — `404 Not Found` for `/admin/users` (router doesn't exist yet)

- [ ] **Step 3: Create `api/routes/admin.py`**

```python
"""
Admin console API routes.

Every route here is gated by `require_admin` (core/security.py), which
403s any authenticated-but-non-admin caller and 401s an unauthenticated
one. Thin translators, same convention as every other router in this
codebase: validate -> call exactly one AdminService method -> shape the
response.

Endpoints (grown across the admin console plan's phases):
  - GET /admin/users: search/filter user accounts
  - PATCH /admin/users/{user_id}: change is_active/user_type
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from core.dependencies import get_admin_service
from core.security import Principal, require_admin
from schemas.admin import AdminUpdateUserRequest, AdminUserListResponse
from schemas.auth import UserProfileResponse
from services.admin_service import AdminService

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=AdminUserListResponse)
async def list_users(
    query: Optional[str] = Query(default=None),
    user_type: Optional[str] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> AdminUserListResponse:
    """GET /admin/users?query=&user_type=&is_active= - search/filter every
    user account. `query` matches case-insensitively against email/full_name.

    Errors: 401 Unauthorized; 403 forbidden if the caller is not an admin.
    """
    users = await service.list_users(query=query, user_type=user_type, is_active=is_active)
    return AdminUserListResponse.from_records(users)


@router.patch("/users/{user_id}", response_model=UserProfileResponse)
async def update_user(
    user_id: str,
    request: AdminUpdateUserRequest,
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> UserProfileResponse:
    """PATCH /admin/users/{user_id} - change `is_active` and/or `user_type`.

    Errors: 401 Unauthorized; 403 forbidden if the caller is not an admin;
    400 invalid_request if `user_type` is present and not one of
    candidate/recruiter/admin; 404 not_found if no such user exists.
    """
    updates = request.model_dump(exclude_unset=True)
    user = await service.update_user(user_id, updates)
    return UserProfileResponse.from_record(user)


__all__ = ["router"]
```

- [ ] **Step 4: Wire `get_admin_service` and register the router**

In `core/dependencies.py`, add the import and the function:

```python
from services.admin_service import AdminService
```

```python
def get_admin_service(request: Request) -> AdminService:
    container = _container_from_app(request.app)
    return AdminService(
        user_repository=container.user_repository,
        job_repository=container.job_repository,
        candidate_repository=container.candidate_repository,
        application_repository=container.application_repository,
        audit_log_repository=container.audit_log_repository,
        auth_service=AuthService(
            user_repository=container.user_repository, settings=container.settings
        ),
    )
```

Add `"get_admin_service"` to `__all__`.

In `api/app.py`, add the import and registration:

```python
from api.routes.admin import router as admin_router
```

```python
    app.include_router(admin_router, prefix=settings.api_prefix)
```

(add this line alongside the other `app.include_router(..., prefix=settings.api_prefix)` calls, e.g. right after the `auth_router` line)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_backend_admin.py -v`
Expected: PASS

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `pytest tests/ -v -k "not postgres"`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add api/routes/admin.py api/app.py core/dependencies.py tests/test_backend_admin.py
git commit -m "feat(admin): add GET/PATCH /admin/users and register the admin router"
```

---

## Task 12: `pages/admin.html` — Users tab + navbar link

**Files:**
- Create: `pages/admin.html`
- Modify: `pages/js/app.js` (`renderNavbar`, new admin API helpers)

**Interfaces:**
- Consumes: `apiRequest`, `getCurrentUser`, `renderNavbar` (existing, `pages/js/app.js`); `GET/PATCH /admin/users` (Task 11).
- Produces: `fetchAdminUsers(filters) -> Promise<object>`, `updateAdminUser(userId, fields) -> Promise<object>`, consumed by `pages/admin.html`'s own inline script and later by Tasks 15/17/21's Moderation/Metrics/Impersonation tabs on the SAME page.

Frontend-only, no `pytest` coverage — same manual-verification convention as the profile-page plan's Task 6 (no JS test runner exists in this repo).

- [ ] **Step 1: Add the navbar admin link**

In `pages/js/app.js`, update `renderNavbar` (the function from the profile-page plan's Task 6, currently around line 264) to add an admin-only link. Replace the `nav-links` block:

```javascript
function renderNavbar(activePage = '') {
  const user = getCurrentUser();
  const navContainer = document.getElementById('navbarContainer');
  if (!navContainer) return;

  const isRecruiter = user && user.role === 'recruiter';
  const isAdmin = user && user.role === 'admin';
  const dashboardHref = isRecruiter ? 'recruiter.html' : 'candidate.html';

  navContainer.innerHTML = `
    <header class="navbar">
      <div class="brand-logo" onclick="window.location.href='index.html'">
        <span class="brand-dot"></span>OpenHire
      </div>

      <nav class="nav-links">
        <a href="index.html">Overview</a>
        ${!isAdmin ? `<a href="${dashboardHref}" class="${activePage === 'dashboard' ? 'active' : ''}">Dashboard</a>` : ''}
        ${isRecruiter ? `<a href="create-job.html" class="${activePage === 'create-job' ? 'active' : ''}">+ Job</a>` : ''}
        ${isRecruiter ? `<a href="screening.html" class="${activePage === 'screening' ? 'active' : ''}">Screening</a>` : ''}
        <a href="leaderboard.html" class="${activePage === 'leaderboard' ? 'active' : ''}">Leaderboard</a>
        <a href="openbox.html" class="${activePage === 'openbox' ? 'active' : ''}">OpenBox</a>
        ${isAdmin ? `<a href="admin.html" class="${activePage === 'admin' ? 'active' : ''}">Admin</a>` : ''}
      </nav>

      <div class="nav-right">
        ${user ? `
          <div class="nav-menu">
            <button type="button" class="user-badge nav-menu-trigger" onclick="toggleNavMenu(event)">
              <span>${user.name}</span>
              <span class="role-tag">${user.role}</span>
            </button>
            <div class="nav-menu-dropdown" id="navMenuDropdown" hidden>
              <a href="profile.html">Profile</a>
              <a href="profile.html#api-keys">API Keys</a>
              <div class="nav-menu-divider"></div>
              <button type="button" class="nav-menu-item-btn" onclick="logoutUser()">Sign out</button>
            </div>
          </div>
        ` : `
          <button class="btn-outline" onclick="window.location.href='login.html'">Sign In</button>
        `}
      </div>
    </header>
  `;
}
```

(`isAdmin` hides the candidate/recruiter "Dashboard" link, since an admin account has neither a candidate nor recruiter dashboard to show, and adds an "Admin" link only it sees — candidates and recruiters never see the link, per spec §6.)

- [ ] **Step 2: Add the admin API helpers**

In `pages/js/app.js`, add after `changeMyPassword` (the profile-page plan's Task 7 helper):

```javascript
/** GET /admin/users with optional {query, user_type, is_active} filters -
 * only ever called from pages/admin.html, only ever reachable by an admin
 * (the navbar link is hidden otherwise, and the backend 403s regardless). */
async function fetchAdminUsers(filters = {}) {
  const params = new URLSearchParams();
  if (filters.query) params.set('query', filters.query);
  if (filters.user_type) params.set('user_type', filters.user_type);
  if (filters.is_active !== undefined && filters.is_active !== '') {
    params.set('is_active', filters.is_active);
  }
  const qs = params.toString();
  return apiRequest(`/admin/users${qs ? '?' + qs : ''}`);
}

/** PATCH /admin/users/{userId} with a partial {is_active, user_type} body. */
async function updateAdminUser(userId, fields) {
  return apiRequest(`/admin/users/${userId}`, { method: 'PATCH', body: fields });
}
```

- [ ] **Step 3: Create `pages/admin.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenHire — Admin Console</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="css/style.css">
</head>
<body>
<div id="navbarContainer"></div>
<div id="impersonationBanner"></div>

<main class="container">
  <div class="card-header" style="margin-bottom:1.5rem;">
    <div>
      <h2 class="card-title">Admin Console</h2>
      <p class="card-sub">User management, content moderation, and platform metrics.</p>
    </div>
  </div>

  <div class="tab-bar" id="adminTabBar">
    <button type="button" class="tab-btn active" data-tab="users">Users</button>
    <button type="button" class="tab-btn" data-tab="moderation">Moderation</button>
    <button type="button" class="tab-btn" data-tab="metrics">Metrics</button>
  </div>

  <section id="usersTab" class="admin-tab">
    <div class="card">
      <div class="card-header">
        <h3 class="card-title" style="font-size:1.05rem;">Users</h3>
      </div>
      <form id="userFilterForm" class="filter-row">
        <input class="form-control" id="userQuery" placeholder="Search email or name">
        <select class="form-control" id="userTypeFilter">
          <option value="">All roles</option>
          <option value="candidate">Candidate</option>
          <option value="recruiter">Recruiter</option>
          <option value="admin">Admin</option>
        </select>
        <select class="form-control" id="userActiveFilter">
          <option value="">Any status</option>
          <option value="true">Active</option>
          <option value="false">Inactive</option>
        </select>
        <button type="submit" class="btn-primary">Search</button>
      </form>
      <table class="data-table" id="usersTable">
        <thead>
          <tr><th>Email</th><th>Role</th><th>Status</th><th>Actions</th></tr>
        </thead>
        <tbody id="usersTableBody"></tbody>
      </table>
      <div class="form-status" id="usersStatus"></div>
    </div>
  </section>

  <section id="moderationTab" class="admin-tab" hidden>
    <!-- Populated by Task 15 (Moderation API + UI). -->
  </section>

  <section id="metricsTab" class="admin-tab" hidden>
    <!-- Populated by Task 17 (Metrics API + UI). -->
  </section>
</main>

<script src="js/auth.js"></script>
<script src="js/app.js"></script>
<script>
guardAuthenticatedPage();

function switchAdminTab(name) {
  document.querySelectorAll('.admin-tab').forEach((el) => { el.hidden = true; });
  document.querySelectorAll('#adminTabBar .tab-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.tab === name);
  });
  const el = document.getElementById(`${name}Tab`);
  if (el) el.hidden = false;
}

document.getElementById('adminTabBar').addEventListener('click', (event) => {
  const btn = event.target.closest('.tab-btn');
  if (btn) switchAdminTab(btn.dataset.tab);
});

function roleDropdown(currentType) {
  const roles = ['candidate', 'recruiter', 'admin'];
  return `<select class="form-control user-role-select">${roles.map(
    (r) => `<option value="${r}" ${r === currentType ? 'selected' : ''}>${r}</option>`
  ).join('')}</select>`;
}

function renderUsersTable(users) {
  const tbody = document.getElementById('usersTableBody');
  tbody.innerHTML = users.map((u) => `
    <tr data-user-id="${u.user_id}">
      <td>${u.email}</td>
      <td>${roleDropdown(u.user_type)}</td>
      <td>${u.is_active ? 'Active' : 'Inactive'}</td>
      <td>
        <button type="button" class="btn-outline toggle-active-btn">${u.is_active ? 'Deactivate' : 'Reactivate'}</button>
        <button type="button" class="btn-outline save-role-btn">Save role</button>
        <button type="button" class="btn-outline impersonate-btn">Impersonate</button>
      </td>
    </tr>
  `).join('');
}

async function loadUsers() {
  const status = document.getElementById('usersStatus');
  status.textContent = '';
  try {
    const filters = {
      query: document.getElementById('userQuery').value.trim(),
      user_type: document.getElementById('userTypeFilter').value,
      is_active: document.getElementById('userActiveFilter').value,
    };
    const data = await fetchAdminUsers(filters);
    renderUsersTable(data.users);
  } catch (err) {
    status.textContent = err.message || 'Failed to load users.';
    status.className = 'form-status error';
  }
}

document.getElementById('userFilterForm').addEventListener('submit', (event) => {
  event.preventDefault();
  loadUsers();
});

document.getElementById('usersTableBody').addEventListener('click', async (event) => {
  const row = event.target.closest('tr[data-user-id]');
  if (!row) return;
  const userId = row.dataset.userId;
  const status = document.getElementById('usersStatus');

  try {
    if (event.target.classList.contains('toggle-active-btn')) {
      const wantsActive = event.target.textContent.trim() === 'Reactivate';
      await updateAdminUser(userId, { is_active: wantsActive });
      await loadUsers();
    } else if (event.target.classList.contains('save-role-btn')) {
      const select = row.querySelector('.user-role-select');
      await updateAdminUser(userId, { user_type: select.value });
      await loadUsers();
    }
    status.textContent = 'Saved.';
    status.className = 'form-status success';
  } catch (err) {
    status.textContent = err.message || 'Action failed.';
    status.className = 'form-status error';
  }
});

loadUsers();
</script>
</body>
</html>
```

(The `impersonate-btn` click handler is added in Task 21, once `POST /admin/impersonate/{id}` exists — the button renders now so Task 21 only adds behavior, not markup.)

- [ ] **Step 4: Manual verification**

With the backend running and a promoted admin account (via `scripts/promote_admin.py`, Task 8) logged in via `login.html`, open `pages/admin.html`. Confirm: the Users table loads, filtering by role/status narrows it, "Deactivate"/"Reactivate" and the role dropdown's "Save role" both work and are reflected on reload. Log in as a candidate/recruiter and confirm the "Admin" navbar link does not appear.

- [ ] **Step 5: Commit**

```bash
git add pages/admin.html pages/js/app.js
git commit -m "feat(admin): add pages/admin.html Users tab and gate the navbar Admin link"
```

---

## Task 13: Moderation — jobs (`AdminService` + routes)

**Files:**
- Modify: `services/admin_service.py`
- Modify: `api/routes/admin.py`
- Test: `tests/test_backend_admin.py`

**Interfaces:**
- Consumes: `JobRepository.list_jobs`/`.archive`/`.restore` (existing + Task 6).
- Produces: `AdminService.list_jobs(include_hidden: bool = False) -> list[JobRecord]`; `AdminService.set_job_active(job_id, is_active) -> JobRecord` (raises `NotFoundError`); `GET/PATCH /admin/jobs`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backend_admin.py`:

```python
# ---------------------------------------------------------------------------
# AdminService: job moderation
# ---------------------------------------------------------------------------

class TestAdminServiceJobModeration:
    @pytest.mark.asyncio
    async def test_list_jobs_excludes_archived_by_default(self):
        jobs = _Jobs()
        await jobs.save(JobRecord(job_id="job_1", job=_bare_job("job_1")))
        await jobs.archive("job_1")
        service = AdminService(
            user_repository=InMemoryUserRepository(), job_repository=jobs,
            candidate_repository=_Candidates(), application_repository=_Applications(),
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=InMemoryUserRepository(), settings=AppSettings()),
        )
        assert await service.list_jobs() == []
        assert len(await service.list_jobs(include_hidden=True)) == 1

    @pytest.mark.asyncio
    async def test_set_job_active_false_archives(self):
        jobs = _Jobs()
        await jobs.save(JobRecord(job_id="job_1", job=_bare_job("job_1")))
        service = AdminService(
            user_repository=InMemoryUserRepository(), job_repository=jobs,
            candidate_repository=_Candidates(), application_repository=_Applications(),
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=InMemoryUserRepository(), settings=AppSettings()),
        )
        updated = await service.set_job_active("job_1", False)
        assert updated.is_active is False

    @pytest.mark.asyncio
    async def test_set_job_active_true_restores(self):
        jobs = _Jobs()
        await jobs.save(JobRecord(job_id="job_1", job=_bare_job("job_1")))
        await jobs.archive("job_1")
        service = AdminService(
            user_repository=InMemoryUserRepository(), job_repository=jobs,
            candidate_repository=_Candidates(), application_repository=_Applications(),
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=InMemoryUserRepository(), settings=AppSettings()),
        )
        updated = await service.set_job_active("job_1", True)
        assert updated.is_active is True

    @pytest.mark.asyncio
    async def test_set_job_active_unknown_job_is_not_found(self):
        service = AdminService(
            user_repository=InMemoryUserRepository(), job_repository=_Jobs(),
            candidate_repository=_Candidates(), application_repository=_Applications(),
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=InMemoryUserRepository(), settings=AppSettings()),
        )
        with pytest.raises(NotFoundError):
            await service.set_job_active("does_not_exist", False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_admin.py -v`
Expected: FAIL — `AttributeError: 'AdminService' object has no attribute 'list_jobs'`

- [ ] **Step 3: Add the methods to `AdminService`**

In `services/admin_service.py`, add after the user-management methods:

```python
    # -- Moderation: jobs -------------------------------------------------

    async def list_jobs(self, *, include_hidden: bool = False) -> list[JobRecord]:
        return await self._jobs.list_jobs(include_archived=include_hidden)

    async def set_job_active(self, job_id: str, is_active: bool) -> JobRecord:
        """`is_active=False` hides the job (delegates to the existing
        `JobRepository.archive` - indistinguishable from a recruiter's own
        archive action, per spec §3.2/§8); `is_active=True` un-hides it
        (`JobRepository.restore`, Task 6).

        Raises:
            NotFoundError: If no such job exists.
        """
        updated = await self._jobs.archive(job_id) if not is_active else await self._jobs.restore(job_id)
        if updated is None:
            raise NotFoundError(
                "No job found.",
                internal_detail=f"job_id={job_id!r} not found",
            )
        logger.info(
            "admin set job active state",
            extra=log_context(event="admin_job_active_set", job_id=job_id, is_active=is_active),
        )
        return updated
```

- [ ] **Step 4: Add the routes**

In `api/routes/admin.py`, add the imports and routes:

```python
from api.models_jobs import JobListResponse, JobResponse
from schemas.admin import AdminSetJobActiveRequest
```

```python
@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    include_hidden: bool = Query(default=False),
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> JobListResponse:
    """GET /admin/jobs?include_hidden=true - every job, including archived/
    admin-hidden ones when asked for."""
    records = await service.list_jobs(include_hidden=include_hidden)
    return JobListResponse.from_records(records)


@router.patch("/jobs/{job_id}", response_model=JobResponse)
async def set_job_active(
    job_id: str,
    request: AdminSetJobActiveRequest,
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> JobResponse:
    """PATCH /admin/jobs/{job_id} - {"is_active": false} hides the job,
    {"is_active": true} restores it. Errors: 404 not_found."""
    record = await service.set_job_active(job_id, request.is_active)
    return JobResponse.from_record(record)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_backend_admin.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add services/admin_service.py api/routes/admin.py tests/test_backend_admin.py
git commit -m "feat(admin): add job moderation to AdminService and GET/PATCH /admin/jobs"
```

---

## Task 14: Moderation — candidates (`AdminService` + routes)

**Files:**
- Modify: `services/admin_service.py`
- Modify: `api/routes/admin.py`
- Test: `tests/test_backend_admin.py`

**Interfaces:**
- Consumes: `CandidateRepository.list_candidates`/`.set_hidden` (Task 4).
- Produces: `AdminService.list_candidates(include_hidden=False) -> list[CandidateRecord]`; `AdminService.set_candidate_hidden(candidate_id, is_hidden) -> CandidateRecord` (raises `NotFoundError`); `GET/PATCH /admin/candidates`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backend_admin.py`:

```python
# ---------------------------------------------------------------------------
# AdminService: candidate moderation
# ---------------------------------------------------------------------------

def _admin_service_full():
    settings = AppSettings()
    user_repo = InMemoryUserRepository()
    return AdminService(
        user_repository=user_repo, job_repository=_Jobs(),
        candidate_repository=_Candidates(), application_repository=_Applications(),
        audit_log_repository=InMemoryAuditLogRepository(),
        auth_service=AuthService(user_repository=user_repo, settings=settings),
    )


class TestAdminServiceCandidateModeration:
    @pytest.mark.asyncio
    async def test_set_candidate_hidden_true_hides(self):
        candidates = _Candidates()
        await candidates.save(CandidateRecord(
            candidate_id="cand_1", user_id="user_1", resume=_bare_resume("cand_1"),
        ))
        service = AdminService(
            user_repository=InMemoryUserRepository(), job_repository=_Jobs(),
            candidate_repository=candidates, application_repository=_Applications(),
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=InMemoryUserRepository(), settings=AppSettings()),
        )
        updated = await service.set_candidate_hidden("cand_1", True)
        assert updated.is_hidden is True
        restored = await service.set_candidate_hidden("cand_1", False)
        assert restored.is_hidden is False

    @pytest.mark.asyncio
    async def test_set_candidate_hidden_unknown_is_not_found(self):
        service = _admin_service_full()
        with pytest.raises(NotFoundError):
            await service.set_candidate_hidden("does_not_exist", True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_admin.py -v`
Expected: FAIL — `AttributeError: 'AdminService' object has no attribute 'set_candidate_hidden'`

- [ ] **Step 3: Add the methods to `AdminService`**

```python
    # -- Moderation: candidates --------------------------------------------

    async def list_candidates(self, *, include_hidden: bool = False) -> list[CandidateRecord]:
        return await self._candidates.list_candidates(include_hidden=include_hidden)

    async def set_candidate_hidden(self, candidate_id: str, is_hidden: bool) -> CandidateRecord:
        """Raises:
            NotFoundError: If no such candidate exists.
        """
        updated = await self._candidates.set_hidden(candidate_id, is_hidden)
        if updated is None:
            raise NotFoundError(
                "No candidate found.",
                internal_detail=f"candidate_id={candidate_id!r} not found",
            )
        logger.info(
            "admin set candidate hidden state",
            extra=log_context(
                event="admin_candidate_hidden_set", candidate_id=candidate_id, is_hidden=is_hidden
            ),
        )
        return updated
```

- [ ] **Step 4: Add the routes**

In `api/routes/admin.py`, add the imports and routes:

```python
from api.models_candidates import CandidateListResponse, CandidateResponse
from schemas.admin import AdminSetHiddenRequest
```

```python
@router.get("/candidates", response_model=CandidateListResponse)
async def list_candidates(
    include_hidden: bool = Query(default=False),
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> CandidateListResponse:
    """GET /admin/candidates?include_hidden=true - every candidate,
    including admin-hidden ones when asked for."""
    records = await service.list_candidates(include_hidden=include_hidden)
    return CandidateListResponse.from_records(records)


@router.patch("/candidates/{candidate_id}", response_model=CandidateResponse)
async def set_candidate_hidden(
    candidate_id: str,
    request: AdminSetHiddenRequest,
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> CandidateResponse:
    """PATCH /admin/candidates/{candidate_id} - {"is_hidden": bool}.
    Errors: 404 not_found."""
    record = await service.set_candidate_hidden(candidate_id, request.is_hidden)
    return CandidateResponse.from_record(record)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_backend_admin.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add services/admin_service.py api/routes/admin.py tests/test_backend_admin.py
git commit -m "feat(admin): add candidate moderation to AdminService and GET/PATCH /admin/candidates"
```

---

## Task 15: Moderation — applications (`AdminService` + routes)

**Files:**
- Modify: `services/admin_service.py`
- Modify: `api/routes/admin.py`
- Test: `tests/test_backend_admin.py`

**Interfaces:**
- Consumes: `ApplicationRepository.list_all`/`.set_hidden` (Task 5).
- Produces: `AdminService.list_applications(include_hidden=False) -> list[Application]`; `AdminService.set_application_hidden(application_id, is_hidden) -> Application` (raises `NotFoundError`); `GET/PATCH /admin/applications`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backend_admin.py`:

```python
# ---------------------------------------------------------------------------
# AdminService: application moderation
# ---------------------------------------------------------------------------

class TestAdminServiceApplicationModeration:
    @pytest.mark.asyncio
    async def test_list_applications_excludes_hidden_by_default(self):
        applications = _Applications()
        await applications.save(Application(application_id="app_1", job_id="job_1", candidate_id="cand_1"))
        await applications.save(Application(
            application_id="app_2", job_id="job_1", candidate_id="cand_2", is_hidden=True,
        ))
        service = AdminService(
            user_repository=InMemoryUserRepository(), job_repository=_Jobs(),
            candidate_repository=_Candidates(), application_repository=applications,
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=InMemoryUserRepository(), settings=AppSettings()),
        )
        assert [a.application_id for a in await service.list_applications()] == ["app_1"]
        assert len(await service.list_applications(include_hidden=True)) == 2

    @pytest.mark.asyncio
    async def test_set_application_hidden_toggles(self):
        applications = _Applications()
        await applications.save(Application(application_id="app_1", job_id="job_1", candidate_id="cand_1"))
        service = AdminService(
            user_repository=InMemoryUserRepository(), job_repository=_Jobs(),
            candidate_repository=_Candidates(), application_repository=applications,
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=InMemoryUserRepository(), settings=AppSettings()),
        )
        hidden = await service.set_application_hidden("app_1", True)
        assert hidden.is_hidden is True

    @pytest.mark.asyncio
    async def test_set_application_hidden_unknown_is_not_found(self):
        service = _admin_service_full()
        with pytest.raises(NotFoundError):
            await service.set_application_hidden("does_not_exist", True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_admin.py -v`
Expected: FAIL — `AttributeError: 'AdminService' object has no attribute 'list_applications'`

- [ ] **Step 3: Add the methods to `AdminService`**

```python
    # -- Moderation: applications -------------------------------------------

    async def list_applications(self, *, include_hidden: bool = False) -> list[Application]:
        return await self._applications.list_all(include_hidden=include_hidden)

    async def set_application_hidden(self, application_id: str, is_hidden: bool) -> Application:
        """Raises:
            NotFoundError: If no such application exists.
        """
        updated = await self._applications.set_hidden(application_id, is_hidden)
        if updated is None:
            raise NotFoundError(
                "No application found.",
                internal_detail=f"application_id={application_id!r} not found",
            )
        logger.info(
            "admin set application hidden state",
            extra=log_context(
                event="admin_application_hidden_set",
                application_id=application_id, is_hidden=is_hidden,
            ),
        )
        return updated
```

- [ ] **Step 4: Add the routes**

In `api/routes/admin.py`, add the imports and routes:

```python
from api.models_applications import ApplicationListResponse, ApplicationResponse
```

```python
@router.get("/applications", response_model=ApplicationListResponse)
async def list_applications(
    include_hidden: bool = Query(default=False),
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> ApplicationListResponse:
    """GET /admin/applications?include_hidden=true - every application on
    the platform, including admin-hidden ones when asked for."""
    applications = await service.list_applications(include_hidden=include_hidden)
    return ApplicationListResponse.from_domain(applications)


@router.patch("/applications/{application_id}", response_model=ApplicationResponse)
async def set_application_hidden(
    application_id: str,
    request: AdminSetHiddenRequest,
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> ApplicationResponse:
    """PATCH /admin/applications/{application_id} - {"is_hidden": bool}.
    Errors: 404 not_found."""
    application = await service.set_application_hidden(application_id, request.is_hidden)
    return ApplicationResponse.from_domain(application)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_backend_admin.py -v`
Expected: PASS

Run: `pytest tests/ -v -k "not postgres"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add services/admin_service.py api/routes/admin.py tests/test_backend_admin.py
git commit -m "feat(admin): add application moderation to AdminService and GET/PATCH /admin/applications"
```

---

## Task 16: Moderation tab UI

**Files:**
- Modify: `pages/admin.html`
- Modify: `pages/js/app.js`

**Interfaces:**
- Consumes: `apiRequest` (existing); `GET/PATCH /admin/jobs`, `/admin/candidates`, `/admin/applications` (Tasks 13-15).
- Produces: `fetchAdminJobs`/`setAdminJobActive`, `fetchAdminCandidates`/`setAdminCandidateHidden`, `fetchAdminApplications`/`setAdminApplicationHidden` — used only by `pages/admin.html`'s own inline script.

Frontend-only; manual verification, same convention as Task 12.

- [ ] **Step 1: Add the moderation API helpers**

In `pages/js/app.js`, add after the Task 12 admin helpers:

```javascript
async function fetchAdminJobs(includeHidden = false) {
  return apiRequest(`/admin/jobs?include_hidden=${includeHidden}`);
}
async function setAdminJobActive(jobId, isActive) {
  return apiRequest(`/admin/jobs/${jobId}`, { method: 'PATCH', body: { is_active: isActive } });
}
async function fetchAdminCandidates(includeHidden = false) {
  return apiRequest(`/admin/candidates?include_hidden=${includeHidden}`);
}
async function setAdminCandidateHidden(candidateId, isHidden) {
  return apiRequest(`/admin/candidates/${candidateId}`, { method: 'PATCH', body: { is_hidden: isHidden } });
}
async function fetchAdminApplications(includeHidden = false) {
  return apiRequest(`/admin/applications?include_hidden=${includeHidden}`);
}
async function setAdminApplicationHidden(applicationId, isHidden) {
  return apiRequest(`/admin/applications/${applicationId}`, { method: 'PATCH', body: { is_hidden: isHidden } });
}
```

- [ ] **Step 2: Fill in the Moderation tab's markup**

In `pages/admin.html`, replace the `<section id="moderationTab" ...>` placeholder:

```html
  <section id="moderationTab" class="admin-tab" hidden>
    <div class="card">
      <div class="card-header"><h3 class="card-title" style="font-size:1.05rem;">Jobs</h3></div>
      <table class="data-table" id="modJobsTable">
        <thead><tr><th>Title</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody id="modJobsBody"></tbody>
      </table>
    </div>
    <div class="card">
      <div class="card-header"><h3 class="card-title" style="font-size:1.05rem;">Candidates</h3></div>
      <table class="data-table" id="modCandidatesTable">
        <thead><tr><th>Candidate</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody id="modCandidatesBody"></tbody>
      </table>
    </div>
    <div class="card">
      <div class="card-header"><h3 class="card-title" style="font-size:1.05rem;">Applications</h3></div>
      <table class="data-table" id="modApplicationsTable">
        <thead><tr><th>Application ID</th><th>Job</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody id="modApplicationsBody"></tbody>
      </table>
    </div>
    <div class="form-status" id="moderationStatus"></div>
  </section>
```

- [ ] **Step 3: Add the Moderation tab's script**

In `pages/admin.html`'s inline `<script>` block, add after the Users tab logic:

```javascript
async function loadModeration() {
  const status = document.getElementById('moderationStatus');
  status.textContent = '';
  try {
    const [jobs, candidates, applications] = await Promise.all([
      fetchAdminJobs(true), fetchAdminCandidates(true), fetchAdminApplications(true),
    ]);

    document.getElementById('modJobsBody').innerHTML = jobs.jobs.map((j) => `
      <tr data-job-id="${j.job_id}">
        <td>${j.job.title}</td>
        <td>${j.is_active ? 'Active' : 'Hidden'}</td>
        <td><button type="button" class="btn-outline job-toggle-btn">${j.is_active ? 'Hide' : 'Restore'}</button></td>
      </tr>
    `).join('');

    document.getElementById('modCandidatesBody').innerHTML = candidates.candidates.map((c) => `
      <tr data-candidate-id="${c.candidate_id}">
        <td>${c.resume.candidate_name || c.candidate_id}</td>
        <td>${c.is_hidden ? 'Hidden' : 'Visible'}</td>
        <td><button type="button" class="btn-outline candidate-toggle-btn">${c.is_hidden ? 'Restore' : 'Hide'}</button></td>
      </tr>
    `).join('');

    document.getElementById('modApplicationsBody').innerHTML = applications.applications.map((a) => `
      <tr data-application-id="${a.application_id}">
        <td>${a.application_id}</td>
        <td>${a.job_id}</td>
        <td>${a.is_hidden ? 'Hidden' : 'Visible'}</td>
        <td><button type="button" class="btn-outline application-toggle-btn">${a.is_hidden ? 'Restore' : 'Hide'}</button></td>
      </tr>
    `).join('');
  } catch (err) {
    status.textContent = err.message || 'Failed to load moderation data.';
    status.className = 'form-status error';
  }
}

document.getElementById('modJobsBody').addEventListener('click', async (event) => {
  if (!event.target.classList.contains('job-toggle-btn')) return;
  const row = event.target.closest('tr[data-job-id]');
  const wantsRestore = event.target.textContent.trim() === 'Restore';
  await setAdminJobActive(row.dataset.jobId, wantsRestore);
  await loadModeration();
});

document.getElementById('modCandidatesBody').addEventListener('click', async (event) => {
  if (!event.target.classList.contains('candidate-toggle-btn')) return;
  const row = event.target.closest('tr[data-candidate-id]');
  const wantsRestore = event.target.textContent.trim() === 'Restore';
  await setAdminCandidateHidden(row.dataset.candidateId, !wantsRestore);
  await loadModeration();
});

document.getElementById('modApplicationsBody').addEventListener('click', async (event) => {
  if (!event.target.classList.contains('application-toggle-btn')) return;
  const row = event.target.closest('tr[data-application-id]');
  const wantsRestore = event.target.textContent.trim() === 'Restore';
  await setAdminApplicationHidden(row.dataset.applicationId, !wantsRestore);
  await loadModeration();
});
```

Update `switchAdminTab` to lazy-load the Moderation tab's data the first time it is opened, by editing the click handler added in Task 12:

```javascript
let moderationLoaded = false;
document.getElementById('adminTabBar').addEventListener('click', (event) => {
  const btn = event.target.closest('.tab-btn');
  if (!btn) return;
  switchAdminTab(btn.dataset.tab);
  if (btn.dataset.tab === 'moderation' && !moderationLoaded) {
    moderationLoaded = true;
    loadModeration();
  }
});
```

(This replaces the plain `switchAdminTab(btn.dataset.tab)`-only listener Task 12 added.)

- [ ] **Step 4: Manual verification**

As an admin, open the Moderation tab: confirm all three lists load, "Hide"/"Restore" round-trips for each of jobs/candidates/applications, and a hidden job no longer appears on the public `GET /jobs` listing (e.g. `pages/candidate.html`'s job browse) while still appearing here with `include_hidden=true`.

- [ ] **Step 5: Commit**

```bash
git add pages/admin.html pages/js/app.js
git commit -m "feat(admin): add the Moderation tab (jobs/candidates/applications hide-restore)"
```

---

## Task 17: `GET /admin/metrics`

**Files:**
- Modify: `services/admin_service.py`
- Modify: `api/routes/admin.py`
- Test: `tests/test_backend_admin.py`

**Interfaces:**
- Consumes: `UserRepository.list_users`, `JobRepository.list_jobs`, `ApplicationRepository.list_all` (existing/Task 5/9).
- Produces: `AdminService.get_metrics() -> AdminMetricsResponse`-shaped dict `{"users": {...}, "jobs": {...}, "applications": {...}}`; `GET /admin/metrics`.

Per spec §5.3, counting via existing `list_*` methods is acceptable for phase 1's simple counts — no new `count` methods are added here.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backend_admin.py`:

```python
# ---------------------------------------------------------------------------
# AdminService.get_metrics
# ---------------------------------------------------------------------------

class TestAdminServiceMetrics:
    @pytest.mark.asyncio
    async def test_counts_users_jobs_and_applications(self):
        user_repo = InMemoryUserRepository()
        await user_repo.save(UserRecord(
            user_id="u1", email="a@example.com", password_hash="h", user_type="candidate",
        ))
        await user_repo.save(UserRecord(
            user_id="u2", email="b@example.com", password_hash="h", user_type="recruiter",
        ))
        await user_repo.save(UserRecord(
            user_id="u3", email="c@example.com", password_hash="h", user_type="admin",
        ))

        jobs = _Jobs()
        await jobs.save(JobRecord(job_id="job_1", job=_bare_job("job_1")))
        await jobs.save(JobRecord(job_id="job_2", job=_bare_job("job_2")))
        await jobs.archive("job_2")

        applications = _Applications()
        await applications.save(Application(application_id="app_1", job_id="job_1", candidate_id="cand_1"))
        await applications.save(Application(
            application_id="app_2", job_id="job_1", candidate_id="cand_2", is_hidden=True,
        ))

        service = AdminService(
            user_repository=user_repo, job_repository=jobs,
            candidate_repository=_Candidates(), application_repository=applications,
            audit_log_repository=InMemoryAuditLogRepository(),
            auth_service=AuthService(user_repository=user_repo, settings=AppSettings()),
        )
        metrics = await service.get_metrics()
        assert metrics["users"] == {"candidate": 1, "recruiter": 1, "admin": 1}
        assert metrics["jobs"] == {"active": 1, "hidden": 1}
        assert metrics["applications"] == {"total": 2, "hidden": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_admin.py -v`
Expected: FAIL — `AttributeError: 'AdminService' object has no attribute 'get_metrics'`

- [ ] **Step 3: Add `get_metrics` to `AdminService`**

```python
    # -- Metrics ------------------------------------------------------------

    async def get_metrics(self) -> dict:
        """Simple platform counts (spec §5.3), computed from existing
        `list_*` methods - no new aggregation infrastructure. Acceptable per
        spec for phase 1's scale; a future revision can add dedicated
        `count` methods to the repositories if these lists grow large enough
        to make full fetches expensive."""
        users = await self._users.list_users()
        user_counts = {"candidate": 0, "recruiter": 0, "admin": 0}
        for u in users:
            if u.user_type in user_counts:
                user_counts[u.user_type] += 1

        jobs = await self._jobs.list_jobs(include_archived=True)
        active_jobs = sum(1 for j in jobs if j.is_active)
        hidden_jobs = len(jobs) - active_jobs

        applications = await self._applications.list_all(include_hidden=True)
        hidden_applications = sum(1 for a in applications if a.is_hidden)

        return {
            "users": user_counts,
            "jobs": {"active": active_jobs, "hidden": hidden_jobs},
            "applications": {"total": len(applications), "hidden": hidden_applications},
        }
```

- [ ] **Step 4: Add the route**

In `api/routes/admin.py`, add the import and route:

```python
from schemas.admin import AdminMetricsResponse
```

```python
@router.get("/metrics", response_model=AdminMetricsResponse)
async def get_metrics(
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> AdminMetricsResponse:
    """GET /admin/metrics - simple platform counts: users by type, jobs
    active/hidden, applications total/hidden."""
    return AdminMetricsResponse.model_validate(await service.get_metrics())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_backend_admin.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add services/admin_service.py api/routes/admin.py tests/test_backend_admin.py
git commit -m "feat(admin): add AdminService.get_metrics and GET /admin/metrics"
```

---

## Task 18: Metrics tab UI

**Files:**
- Modify: `pages/admin.html`
- Modify: `pages/js/app.js`

**Interfaces:**
- Consumes: `apiRequest`; `GET /admin/metrics` (Task 17).
- Produces: `fetchAdminMetrics() -> Promise<object>`, used only by `pages/admin.html`.

- [ ] **Step 1: Add the helper**

In `pages/js/app.js`, add after the Task 16 helpers:

```javascript
async function fetchAdminMetrics() {
  return apiRequest('/admin/metrics');
}
```

- [ ] **Step 2: Fill in the Metrics tab**

In `pages/admin.html`, replace the `<section id="metricsTab" ...>` placeholder:

```html
  <section id="metricsTab" class="admin-tab" hidden>
    <div class="card">
      <div class="card-header"><h3 class="card-title" style="font-size:1.05rem;">Users</h3></div>
      <div class="chip-row" id="metricsUsers"></div>
    </div>
    <div class="card">
      <div class="card-header"><h3 class="card-title" style="font-size:1.05rem;">Jobs</h3></div>
      <div class="chip-row" id="metricsJobs"></div>
    </div>
    <div class="card">
      <div class="card-header"><h3 class="card-title" style="font-size:1.05rem;">Applications</h3></div>
      <div class="chip-row" id="metricsApplications"></div>
    </div>
    <div class="form-status" id="metricsStatus"></div>
  </section>
```

- [ ] **Step 3: Add the Metrics tab's script**

In `pages/admin.html`'s inline `<script>` block:

```javascript
async function loadMetrics() {
  const status = document.getElementById('metricsStatus');
  status.textContent = '';
  try {
    const metrics = await fetchAdminMetrics();
    document.getElementById('metricsUsers').innerHTML = `
      <span class="chip">Candidates: ${metrics.users.candidate}</span>
      <span class="chip">Recruiters: ${metrics.users.recruiter}</span>
      <span class="chip">Admins: ${metrics.users.admin}</span>
    `;
    document.getElementById('metricsJobs').innerHTML = `
      <span class="chip">Active: ${metrics.jobs.active}</span>
      <span class="chip">Hidden: ${metrics.jobs.hidden}</span>
    `;
    document.getElementById('metricsApplications').innerHTML = `
      <span class="chip">Total: ${metrics.applications.total}</span>
      <span class="chip">Hidden: ${metrics.applications.hidden}</span>
    `;
  } catch (err) {
    status.textContent = err.message || 'Failed to load metrics.';
    status.className = 'form-status error';
  }
}
```

Update the tab-switch listener (from Task 16) to lazy-load Metrics too:

```javascript
let moderationLoaded = false;
let metricsLoaded = false;
document.getElementById('adminTabBar').addEventListener('click', (event) => {
  const btn = event.target.closest('.tab-btn');
  if (!btn) return;
  switchAdminTab(btn.dataset.tab);
  if (btn.dataset.tab === 'moderation' && !moderationLoaded) {
    moderationLoaded = true;
    loadModeration();
  }
  if (btn.dataset.tab === 'metrics' && !metricsLoaded) {
    metricsLoaded = true;
    loadMetrics();
  }
});
```

- [ ] **Step 4: Manual verification**

As an admin, open the Metrics tab and confirm the three stat rows show non-fabricated counts matching what the Users/Moderation tabs display.

- [ ] **Step 5: Commit**

```bash
git add pages/admin.html pages/js/app.js
git commit -m "feat(admin): add the Metrics tab"
```

---

## Task 19: `AuthService.issue_tokens_for_user` (public token issuance)

**Files:**
- Modify: `services/auth_service.py`
- Test: `tests/test_backend_admin.py`

**Interfaces:**
- Consumes: `AuthService._generate_access_token`/`_generate_refresh_token` (existing, private).
- Produces: `AuthService.issue_tokens_for_user(user: UserRecord) -> tuple[str, str]` — a public wrapper, used by Task 20's `AdminService.impersonate`. `login`/`signup` are refactored to call it too, so there is exactly one place tokens get minted from a `UserRecord`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backend_admin.py`:

```python
# ---------------------------------------------------------------------------
# AuthService.issue_tokens_for_user
# ---------------------------------------------------------------------------

class TestIssueTokensForUser:
    @pytest.mark.asyncio
    async def test_issues_a_valid_access_and_refresh_token_pair(self):
        user_repo = InMemoryUserRepository()
        service = AuthService(user_repository=user_repo, settings=AppSettings())
        user, _a, _r = await service.signup(
            email="c@example.com", password="password123", user_type="candidate",
        )
        access_token, refresh_token = service.issue_tokens_for_user(user)

        principal = await service.verify_access_token(access_token)
        assert principal.subject_id == user.user_id

        new_access = await service.refresh_access_token(refresh_token)
        assert new_access
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_admin.py -v`
Expected: FAIL — `AttributeError: 'AuthService' object has no attribute 'issue_tokens_for_user'`

- [ ] **Step 3: Add the method and refactor `login`/`signup` to use it**

In `services/auth_service.py`, add after `get_profile`:

```python
    def issue_tokens_for_user(self, user: UserRecord) -> Tuple[str, str]:
        """Mint a fresh access/refresh token pair for an already-resolved
        user, with no password check of its own.

        The single place `login`/`signup` (below) and `AdminService.
        impersonate` (services/admin_service.py) mint tokens from - so an
        impersonated session is byte-for-byte the same shape of token the
        target user's own login produces, exactly as spec §5.4 requires
        ("indistinguishable from the user's own login to the rest of the
        app"). Callers are responsible for their own authorization decision
        BEFORE calling this - it never re-checks a password or an
        is_active flag itself.
        """
        access_token = self._generate_access_token(user.user_id, user.user_type)
        refresh_token = self._generate_refresh_token(user.user_id)
        return access_token, refresh_token
```

Update `signup` to use it (replace its two token-generation lines):

```python
        # Generate tokens
        access_token, refresh_token = self.issue_tokens_for_user(stored_user)
```

Update `login` the same way (replace its two token-generation lines):

```python
        # Generate tokens
        access_token, refresh_token = self.issue_tokens_for_user(user)
```

Update the module docstring's method list to mention it:

```python
"""
Authentication service implementation.

Handles user signup, login, token generation and validation. Integrates with
UserRepository for persistence and AppSettings for configuration.

Methods provided:
  - signup(email, password, user_type): Create a new user account and generate tokens.
  - login(email, password): Authenticate user and return access/refresh tokens.
  - issue_tokens_for_user(user): Mint a token pair for an already-resolved user (no password check).
  - refresh_access_token(refresh_token): Generate a new access token from refresh token.
  - verify_access_token(token): Decode and validate JWT access token, return Principal.
  - get_profile(user_id): Fetch the full stored profile for one user.
  - update_profile(user_id, updates): Apply a partial profile update.
  - change_password(user_id, current_password, new_password): Verify and replace a password.
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_backend_admin.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite to confirm login/signup still work identically**

Run: `pytest tests/ -v -k "not postgres"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add services/auth_service.py tests/test_backend_admin.py
git commit -m "refactor(auth): extract AuthService.issue_tokens_for_user, used by login/signup and impersonation"
```

---

## Task 20: Impersonation (`AdminService` + route + audit log)

**Files:**
- Modify: `services/admin_service.py`
- Modify: `api/routes/admin.py`
- Test: `tests/test_backend_admin.py`

**Interfaces:**
- Consumes: `AuthService.issue_tokens_for_user` (Task 19), `AuditLogRepository.save` (Task 2), `UserRepository.get_by_id`.
- Produces: `AdminService.impersonate(admin_id: str, target_user_id: str) -> tuple[str, str]` (raises `NotFoundError` if the target does not exist, `BadRequestError` if the target is inactive); `POST /admin/impersonate/{user_id}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backend_admin.py`:

```python
# ---------------------------------------------------------------------------
# AdminService.impersonate
# ---------------------------------------------------------------------------

class TestAdminServiceImpersonate:
    @pytest.mark.asyncio
    async def test_issues_tokens_for_the_target_and_writes_one_audit_log_row(self):
        user_repo = InMemoryUserRepository()
        auth_service = AuthService(user_repository=user_repo, settings=AppSettings())
        target, _a, _r = await auth_service.signup(
            email="target@example.com", password="password123", user_type="candidate",
        )
        audit_logs = InMemoryAuditLogRepository()
        service = AdminService(
            user_repository=user_repo, job_repository=_Jobs(),
            candidate_repository=_Candidates(), application_repository=_Applications(),
            audit_log_repository=audit_logs, auth_service=auth_service,
        )

        access_token, refresh_token = await service.impersonate("user_admin_1", target.user_id)

        principal = await auth_service.verify_access_token(access_token)
        assert principal.subject_id == target.user_id

        rows = await audit_logs.list_for_admin("user_admin_1")
        assert len(rows) == 1
        assert rows[0].action == "impersonate"
        assert rows[0].target_user_id == target.user_id

    @pytest.mark.asyncio
    async def test_unknown_target_is_not_found(self):
        service = _admin_service_full()
        with pytest.raises(NotFoundError):
            await service.impersonate("user_admin_1", "does_not_exist")

    @pytest.mark.asyncio
    async def test_inactive_target_is_rejected(self):
        user_repo = InMemoryUserRepository()
        auth_service = AuthService(user_repository=user_repo, settings=AppSettings())
        target, _a, _r = await auth_service.signup(
            email="target@example.com", password="password123", user_type="candidate",
        )
        await user_repo.save(target.model_copy(update={"is_active": False}))
        service = AdminService(
            user_repository=user_repo, job_repository=_Jobs(),
            candidate_repository=_Candidates(), application_repository=_Applications(),
            audit_log_repository=InMemoryAuditLogRepository(), auth_service=auth_service,
        )
        with pytest.raises(BadRequestError):
            await service.impersonate("user_admin_1", target.user_id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_admin.py -v`
Expected: FAIL — `AttributeError: 'AdminService' object has no attribute 'impersonate'`

- [ ] **Step 3: Add `impersonate` to `AdminService`**

```python
    # -- Impersonation --------------------------------------------------

    async def impersonate(self, admin_id: str, target_user_id: str) -> tuple[str, str]:
        """Issue a normal token pair for `target_user_id`, logging exactly
        one `AuditLogRecord` for accountability (spec §5.4).

        Raises:
            NotFoundError: If no such target user exists.
            BadRequestError: If the target account is not active.
        """
        target = await self._users.get_by_id(target_user_id)
        if target is None:
            raise NotFoundError(
                "No account found.",
                internal_detail=f"user_id={target_user_id!r} not found",
            )
        if not target.is_active:
            raise BadRequestError(
                "Cannot impersonate an inactive account.",
                internal_detail=f"user_id={target_user_id!r} is inactive",
            )

        access_token, refresh_token = self._auth.issue_tokens_for_user(target)

        await self._audit_logs.save(AuditLogRecord(
            log_id=f"log_{uuid.uuid4().hex[:12]}",
            admin_id=admin_id,
            action="impersonate",
            target_user_id=target_user_id,
        ))

        logger.info(
            "admin impersonated a user",
            extra=log_context(
                event="admin_impersonate", admin_id=admin_id, target_user_id=target_user_id
            ),
        )
        return access_token, refresh_token
```

- [ ] **Step 4: Add the route**

In `api/routes/admin.py`, add the import and route:

```python
from schemas.admin import ImpersonateResponse
```

```python
@router.post("/impersonate/{user_id}", response_model=ImpersonateResponse)
async def impersonate(
    user_id: str,
    service: AdminService = Depends(get_admin_service),
    principal: Principal = Depends(require_admin),
) -> ImpersonateResponse:
    """POST /admin/impersonate/{user_id} - issue a normal access/refresh
    token pair for the target user, so the resulting session is
    indistinguishable from that user's own login. Writes exactly one
    AuditLogRecord (admin_id=the calling admin, action="impersonate",
    target_user_id=the target).

    Errors: 404 not_found if the target user does not exist; 400
    invalid_request if the target account is inactive.
    """
    access_token, refresh_token = await service.impersonate(principal.subject_id, user_id)
    return ImpersonateResponse(access_token=access_token, refresh_token=refresh_token)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_backend_admin.py -v`
Expected: PASS

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `pytest tests/ -v -k "not postgres"`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add services/admin_service.py api/routes/admin.py tests/test_backend_admin.py
git commit -m "feat(admin): add impersonation - POST /admin/impersonate/{user_id}, audited"
```

---

## Task 21: Frontend impersonation — token swap + banner

**Files:**
- Modify: `pages/admin.html`
- Modify: `pages/js/app.js`
- Modify: `pages/js/auth.js`

**Interfaces:**
- Consumes: `apiRequest`, `setTokens` (existing, `pages/js/auth.js`); `POST /admin/impersonate/{id}` (Task 20); `GET /auth/me` (existing).
- Produces: `impersonateUser(userId) -> Promise<void>` (swaps tokens, redirects); a persistent `#impersonationBanner`, rendered on every page that includes it (only `pages/admin.html` needs the banner markup added in this task, since impersonation is only ever initiated there, but the banner must render on the page the admin gets redirected TO as well — handled by checking `localStorage` in `renderNavbar`, so no other page's HTML needs editing).

- [ ] **Step 1: Add `impersonateUser` and an impersonation-flag helper**

In `pages/js/app.js`, add after the Task 18 helpers:

```javascript
/** POST /admin/impersonate/{userId}, swap stored tokens to the target
 * user's, remember which admin email started this so the banner (below)
 * can show it, and redirect to that user's landing page. */
async function impersonateUser(userId, targetEmail, targetRole) {
  const adminEmail = getCurrentUser() ? getCurrentUser().name : null;
  const response = await apiRequest(`/admin/impersonate/${userId}`, { method: 'POST' });
  setTokens(response.access_token, response.refresh_token);
  setCurrentUser({ name: targetEmail, role: targetRole, user_id: userId });
  if (adminEmail) {
    localStorage.setItem('openhire_impersonating_as_admin', adminEmail);
  }
  window.location.href = targetRole === 'recruiter' ? 'recruiter.html' : 'candidate.html';
}

/** Whether the CURRENT session is an impersonated one - set only by
 * impersonateUser() above, cleared only by returnFromImpersonation(). */
function isImpersonating() {
  return !!localStorage.getItem('openhire_impersonating_as_admin');
}

/** "Return to admin" - the simplest correct behavior for phase 1 (spec
 * §6/§8): re-authenticate as the admin normally, rather than retaining the
 * admin's original tokens across the impersonation session. */
function returnFromImpersonation() {
  localStorage.removeItem('openhire_impersonating_as_admin');
  logoutUser();
}
```

- [ ] **Step 2: Render the banner from `renderNavbar`**

In `pages/js/app.js`, extend `renderNavbar` (from Task 12) to inject the banner markup into `#impersonationBanner` when present on the page:

```javascript
function renderNavbar(activePage = '') {
  const user = getCurrentUser();
  const navContainer = document.getElementById('navbarContainer');
  if (!navContainer) return;

  const isRecruiter = user && user.role === 'recruiter';
  const isAdmin = user && user.role === 'admin';
  const dashboardHref = isRecruiter ? 'recruiter.html' : 'candidate.html';

  navContainer.innerHTML = `
    <header class="navbar">
      <div class="brand-logo" onclick="window.location.href='index.html'">
        <span class="brand-dot"></span>OpenHire
      </div>

      <nav class="nav-links">
        <a href="index.html">Overview</a>
        ${!isAdmin ? `<a href="${dashboardHref}" class="${activePage === 'dashboard' ? 'active' : ''}">Dashboard</a>` : ''}
        ${isRecruiter ? `<a href="create-job.html" class="${activePage === 'create-job' ? 'active' : ''}">+ Job</a>` : ''}
        ${isRecruiter ? `<a href="screening.html" class="${activePage === 'screening' ? 'active' : ''}">Screening</a>` : ''}
        <a href="leaderboard.html" class="${activePage === 'leaderboard' ? 'active' : ''}">Leaderboard</a>
        <a href="openbox.html" class="${activePage === 'openbox' ? 'active' : ''}">OpenBox</a>
        ${isAdmin ? `<a href="admin.html" class="${activePage === 'admin' ? 'active' : ''}">Admin</a>` : ''}
      </nav>

      <div class="nav-right">
        ${user ? `
          <div class="nav-menu">
            <button type="button" class="user-badge nav-menu-trigger" onclick="toggleNavMenu(event)">
              <span>${user.name}</span>
              <span class="role-tag">${user.role}</span>
            </button>
            <div class="nav-menu-dropdown" id="navMenuDropdown" hidden>
              <a href="profile.html">Profile</a>
              <a href="profile.html#api-keys">API Keys</a>
              <div class="nav-menu-divider"></div>
              <button type="button" class="nav-menu-item-btn" onclick="logoutUser()">Sign out</button>
            </div>
          </div>
        ` : `
          <button class="btn-outline" onclick="window.location.href='login.html'">Sign In</button>
        `}
      </div>
    </header>
  `;

  const bannerContainer = document.getElementById('impersonationBanner');
  if (bannerContainer) {
    const adminEmail = localStorage.getItem('openhire_impersonating_as_admin');
    bannerContainer.innerHTML = (user && adminEmail) ? `
      <div class="impersonation-banner">
        Viewing as ${user.name} —
        <button type="button" class="link-btn" onclick="returnFromImpersonation()">Return to admin</button>
      </div>
    ` : '';
  }
}
```

- [ ] **Step 3: Add the banner CSS**

Append to `pages/css/style.css`:

```css
.impersonation-banner {
  background: var(--danger-subtle, #3a1f1f);
  color: var(--danger-text, #ffb4b4);
  text-align: center;
  padding: 0.5rem 1rem;
  font-family: var(--font-mono);
  font-size: 12.5px;
}

.impersonation-banner .link-btn {
  background: none;
  border: none;
  color: inherit;
  text-decoration: underline;
  cursor: pointer;
  font: inherit;
}
```

- [ ] **Step 4: Add `#impersonationBanner` to every page that includes `navbarContainer`**

Every page listing `<div id="navbarContainer"></div>` needs a matching `<div id="impersonationBanner"></div>` immediately after it, so the banner has somewhere to render when that page is the one an impersonated admin lands on:

Run: `grep -rl 'id="navbarContainer"' pages/*.html`

For each file returned, add `<div id="impersonationBanner"></div>` on the line directly after `<div id="navbarContainer"></div>` (already present in `pages/admin.html` from Task 12 — skip it).

- [ ] **Step 5: Wire the "Impersonate" button in the Users table**

In `pages/admin.html`'s inline script, extend the `usersTableBody` click handler (from Task 12):

```javascript
document.getElementById('usersTableBody').addEventListener('click', async (event) => {
  const row = event.target.closest('tr[data-user-id]');
  if (!row) return;
  const userId = row.dataset.userId;
  const status = document.getElementById('usersStatus');

  try {
    if (event.target.classList.contains('toggle-active-btn')) {
      const wantsActive = event.target.textContent.trim() === 'Reactivate';
      await updateAdminUser(userId, { is_active: wantsActive });
      await loadUsers();
    } else if (event.target.classList.contains('save-role-btn')) {
      const select = row.querySelector('.user-role-select');
      await updateAdminUser(userId, { user_type: select.value });
      await loadUsers();
    } else if (event.target.classList.contains('impersonate-btn')) {
      const email = row.querySelector('td').textContent.trim();
      const role = row.querySelector('.user-role-select').value;
      if (!confirm(`Impersonate ${email}? You will be signed in as them until you return to admin.`)) {
        return;
      }
      await impersonateUser(userId, email, role);
      return;
    }
    status.textContent = 'Saved.';
    status.className = 'form-status success';
  } catch (err) {
    status.textContent = err.message || 'Action failed.';
    status.className = 'form-status error';
  }
});
```

- [ ] **Step 6: Manual verification**

As an admin, click "Impersonate" on a candidate row: confirm the browser navigates to `candidate.html` signed in as that candidate, the banner reads "Viewing as {email} — Return to admin", and clicking "Return to admin" logs out and redirects to `login.html` (spec §6's documented flow — the admin then logs back in normally).

- [ ] **Step 7: Commit**

```bash
git add pages/admin.html pages/js/app.js pages/js/auth.js pages/css/style.css pages/*.html
git commit -m "feat(admin): add frontend impersonation - token swap, banner, return-to-admin"
```

---

## Self-Review

**1. Spec coverage**

- §3.1 `user_type` admin value: Task 1 (`PrincipalType.ADMIN`, `AuthService` mapping/scopes); `UserRecord.user_type` needed no schema/column change since it was already a plain, uncontrained `str` column (`repositories/postgres/schema.sql`'s `CHECK (user_type IN ('candidate', 'recruiter'))` constraint DOES need widening — see the fix folded into Task 1 below).
- §3.1 `is_active` reused for deactivation, already enforced at login (`services/auth_service.py:146`) — confirmed pre-existing, unchanged; `require_admin` (Task 7) re-checks it too.
- §3.2 moderation flags: `JobRecord.is_active` reused as-is (Task 6, `restore`); `CandidateRecord.is_hidden` (Task 4); `Application.is_hidden` (Task 5).
- §3.3 `AuditLogRecord`/`AuditLogRepository`: Task 2 (interfaces + in-memory), Task 3 (Postgres + container).
- §4 no self-service admin path: confirmed unchanged (`SignupRequest.user_type` pattern); `scripts/promote_admin.py` (Task 8); `PATCH /admin/users/{id}` promotion (Task 10/11).
- §5 `require_admin`, 403 not 404: Task 7.
- §5.1 `GET/PATCH /admin/users`: Task 9 (`list_users`), Task 10-11.
- §5.2 job/candidate/application moderation + existing listings excluding hidden by default: Tasks 4-6, 13-15 (`GET /candidates` explicitly re-verified in Task 4 Step 6; `list_for_job`/`list_for_candidate` explicitly re-verified in Task 5 Step 7 to keep excluding by default for every existing caller).
- §5.3 `GET /admin/metrics`: Task 17.
- §5.4 impersonation + audit log: Tasks 19-20.
- §6 frontend — Users/Moderation/Metrics tabs, navbar gating, impersonate button + banner: Tasks 12, 16, 18, 21.
- §7 testing — `require_admin` 401/403, each endpoint's success path, role-change validation, impersonation's one audit row + valid tokens, moderation round-trips: covered across Tasks 7, 10-20's test steps.
- §8 open questions: both explicitly left as-is (job hide/archive share one flag; "return to admin" re-authenticates) — reflected in the Global Constraints and Task 21 Step 6.

**Fix folded in from this review — Task 1 addendum:** `repositories/postgres/schema.sql`'s `users` table has `user_type text NOT NULL CHECK (user_type IN ('candidate', 'recruiter'))` (line 62), which would reject an admin row at the database level even though every application-layer change in this plan accepts `'admin'`. Add to Task 1, as a new Step 7 (after Step 6's commit — add these to the SAME commit instead by re-opening it before pushing, or as an immediate follow-up commit; either is fine since nothing before Task 9 depends on Postgres accepting the value):

- [ ] **Task 1, Step 7 (addendum): widen the Postgres `user_type` constraint**

In `repositories/postgres/schema.sql`, update the `users` table definition:

```sql
    user_type text NOT NULL CHECK (user_type IN ('candidate', 'recruiter', 'admin')),
```

And add a guarded migration for already-provisioned databases, directly after the table definition (same pattern as the `applications.status` constraint migration later in the file):

```sql
-- Widen users.user_type to allow 'admin' for databases created before the
-- admin console existed (2026-09-06).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables WHERE table_name = 'users'
    ) THEN
        ALTER TABLE users DROP CONSTRAINT IF EXISTS users_user_type_check;
        ALTER TABLE users ADD CONSTRAINT users_user_type_check
            CHECK (user_type IN ('candidate', 'recruiter', 'admin'));
    END IF;
END $$;
```

Commit this together with Task 1's other changes (amend the working tree before that task's commit step if executing sequentially, or land it as its own immediately-following commit — either is acceptable since no later task depends on which).

**2. Placeholder scan** — searched for "TBD"/"TODO"/"implement later"/"add appropriate"/"similar to Task N": none found. Every step that touches code shows the exact code, not a description of it. The one deliberately-deferred item (audit-log viewer UI) is explicitly named as out of scope (Global Constraints), not left as an implicit gap.

**3. Type consistency** — verified across tasks:
- `AuditLogRecord(log_id, admin_id, action, target_user_id, created_at)` — identical field names/order in Task 2 (interfaces/memory), Task 3 (Postgres), Task 20 (`AdminService.impersonate`'s construction call).
- `CandidateRepository.list_candidates(*, include_hidden: bool = False)` and `.set_hidden(candidate_id, is_hidden) -> Optional[CandidateRecord]` — identical signatures in Task 4 (interfaces/memory/Postgres), Task 14 (`AdminService`).
- `ApplicationRepository.list_for_job`/`.list_for_candidate(*, include_hidden: bool = False)`, `.list_all(*, include_hidden: bool = False)`, `.set_hidden(application_id, is_hidden) -> Optional[Application]` — identical across Task 5 (interfaces/memory/Postgres) and Task 15 (`AdminService`).
- `JobRepository.restore(job_id) -> Optional[JobRecord]` — identical across Task 6 and Task 13.
- `UserRepository.list_users(*, query=None, user_type=None, is_active=None)` — identical across Task 9 and Task 10/11's `AdminService.list_users`/route.
- `AdminService.__init__`'s five keyword arguments (`user_repository`, `job_repository`, `candidate_repository`, `application_repository`, `audit_log_repository`, `auth_service`) — used identically in every test fixture from Task 10 onward and in Task 11's `get_admin_service`.
- `AuthService.issue_tokens_for_user(user: UserRecord) -> Tuple[str, str]` — matches its Task 19 definition and its Task 20 call site (`self._auth.issue_tokens_for_user(target)`).
- `require_admin`'s dependency chain (`Depends(require_authenticated)` → `Depends(get_settings)` → `Depends(_get_user_repository_for_security)`) matches `require_authenticated`'s own existing chain shape exactly (Task 7).
- Frontend: `user.role` (from `getCurrentUser()`) is the same string `pages/login.html` already stores from `response.user.user_type` — `'admin'` flows through unchanged, matching `renderNavbar`'s `isAdmin` check (Task 12/21) with no separate frontend vocabulary invented for it.
