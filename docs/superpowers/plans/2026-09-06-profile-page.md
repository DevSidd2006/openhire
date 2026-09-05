# Profile Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every signed-in user a profile page, reached from a top-right dropdown, that shows and edits their account details, shows role-specific information, lets them change their password, and reserves a placeholder card for the BYOK section that the `byok` branch will fill in.

**Architecture:** Extend the existing `users` table and `UserRecord` with nullable profile columns (account-level only — candidate resume data stays on `ParsedResume`/`CandidateRecord`, never duplicated). Add three endpoints to the existing `auth` router, backed by new `AuthService` methods that reuse `UserRepository.save()` (no new repository method needed — fetch, `model_copy`, save). Add `pages/profile.html` built from the existing `.card`/`.form-control` vocabulary, and turn the navbar's user badge into a dropdown in `pages/js/app.js`.

**Tech Stack:** FastAPI, Pydantic v2, `asyncpg` (Postgres), vanilla JS/HTML/CSS (no framework), `pytest` + `fastapi.testclient.TestClient`.

**Spec:** `docs/superpowers/specs/2026-09-06-profile-page-design.md`

## Global Constraints

- No BYOK key storage, encryption, or validation in this plan — that is the `byok` branch's job. This plan ships only an inert placeholder card.
- Candidate skills/experience/education are never copied onto `users` — they are read from `GET /candidates/me` and rendered read-only. `CandidateRecord`'s docstring (`repositories/interfaces.py:378`) is explicit that the resume IS the candidate profile everywhere else in the codebase.
- `email`, `user_type`, `is_active`, `password_hash` are never patchable through the new profile endpoints.
- All new `users` columns are nullable — no backfill migration needed, existing rows are valid as-is.
- New endpoints live on the existing `auth` router (`api/routes/auth.py`), gated by `require_authenticated` (`core/security.py:227`), resolving the subject from `principal.subject_id` — never a client-supplied id.
- Jobs have no recruiter-ownership column in this codebase today (`repositories/interfaces.py:332`, `repositories/postgres/schema.sql:41` — `jobs` carries no owner/recruiter id). The spec's "N jobs posted" recruiter stat is **not implementable** without inventing that ownership link, which is out of scope here. This plan ships the recruiter company fields but omits the jobs-posted counter; flagged again at the point it would have been built.
- Follow existing patterns: `AppError` subclasses for errors (`core/errors.py`), routes stay thin and delegate to services, `apiRequest()` in `pages/js/app.js` for all frontend calls.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `repositories/postgres/schema.sql` | Modify | Add nullable profile columns to `users` |
| `repositories/interfaces.py` | Modify | Add same fields to `UserRecord` |
| `repositories/postgres/user_repository.py` | Modify | Read/write the new columns in `save`/`get_by_email`/`get_by_id` |
| `schemas/auth.py` | Modify | Add `UserProfileResponse`, `UpdateProfileRequest`, `ChangePasswordRequest` |
| `services/auth_service.py` | Modify | Add `get_profile`, `update_profile`, `change_password` |
| `api/routes/auth.py` | Modify | Add `GET/PATCH /auth/me`, `POST /auth/me/password` |
| `pages/css/style.css` | Modify | Avatar circle, nav dropdown, profile page utility classes |
| `pages/js/app.js` | Modify | Dropdown-ify `renderNavbar`; add `fetchMyProfile`/`updateMyProfile`/`changeMyPassword` |
| `pages/profile.html` | Create | The profile page itself |
| `tests/test_backend_profile.py` | Create | Backend tests for the three new endpoints |

`repositories/memory.py` needs **no change** — `InMemoryUserRepository.save()` already stores whatever `UserRecord` it is given via `model_copy`, so the new fields flow through automatically.

---

## Task 1: Extend `UserRecord` and the `users` table schema

**Files:**
- Modify: `repositories/interfaces.py:440-456` (`UserRecord`)
- Modify: `repositories/postgres/schema.sql:58-69` (`users` table)
- Test: `tests/test_backend_profile.py` (new file, starts here)

**Interfaces:**
- Produces: `UserRecord` gains these `Optional[str] = None` fields, used by every later task: `full_name`, `phone`, `location`, `headline`, `bio`, `avatar_url`, `company_name`, `company_website`, `company_role`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_backend_profile.py` with its module docstring and first test, exercising `UserRecord` directly (no HTTP yet):

```python
"""
Profile page backend: account-level profile fields on `users`, and the
GET/PATCH /auth/me + POST /auth/me/password endpoints.

Candidate resume data (skills, experience, education) is deliberately NOT
duplicated here - it stays owned by `CandidateRecord`/`ParsedResume` and is
only ever read, never written, through this module's endpoints. See
`repositories/interfaces.py`'s `CandidateRecord` docstring for why.

BYOK key storage is out of scope for this module - see the `byok` branch.
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.config import AppSettings
from core.container import ServiceContainer
from core.security import JWTAuthProvider
from repositories.interfaces import UserRecord
from repositories.memory import (
    InMemoryApplicationRepository,
    InMemoryBugReportRepository,
    InMemoryCandidateRepository,
    InMemoryEvaluationRepository,
    InMemoryJobRepository,
    InMemoryRubricRepository,
    InMemorySessionRepository,
    InMemoryTranscriptRepository,
    InMemoryUserRepository,
)
from services.auth_service import AuthService
from services.evaluation_dispatcher import AsyncTaskEvaluationDispatcher


class TestUserRecordProfileFields:
    def test_profile_fields_default_to_none_and_round_trip(self):
        """A UserRecord built the way signup builds it today (no profile
        fields supplied) must still validate - these columns are additive,
        not required."""
        record = UserRecord(
            user_id="user_1",
            email="a@example.com",
            password_hash="hash",
            user_type="candidate",
        )
        assert record.full_name is None
        assert record.company_name is None

        updated = record.model_copy(update={"full_name": "Jane Doe", "phone": "555-0100"})
        assert updated.full_name == "Jane Doe"
        assert updated.phone == "555-0100"
        # Untouched fields still round-trip.
        assert updated.email == "a@example.com"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_profile.py -v`
Expected: FAIL with `TypeError` or a Pydantic validation-style error mentioning `full_name` is not a recognised field on `UserRecord` (extra fields rejected, or `AttributeError: 'UserRecord' object has no attribute 'full_name'`).

- [ ] **Step 3: Add the fields to `UserRecord`**

In `repositories/interfaces.py`, extend `UserRecord` (currently at line 440):

```python
class UserRecord(BaseModel):
    """Durable storage record for one user account.

    Stores authentication credentials and account metadata. The password_hash
    field contains the output of a proper password hashing algorithm (e.g.
    bcrypt, scrypt), never plaintext.

    The profile fields below (full_name onward) are account-level only.
    Candidate resume data - skills, experience, education - is deliberately
    NOT stored here: it lives on `CandidateRecord`/`ParsedResume`, which is
    already the single candidate representation every agent and the
    matching/interview engines key off. Duplicating it here would create a
    second copy that drifts the moment a candidate uploads a new resume.
    """

    model_config = ConfigDict(frozen=False)

    user_id: str
    email: str
    password_hash: str
    user_type: str  # 'candidate' or 'recruiter'
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None

    # Account-level profile fields, shared by both roles.
    full_name: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    headline: Optional[str] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None

    # Recruiter-only fields. Left None for a candidate account.
    company_name: Optional[str] = None
    company_website: Optional[str] = None
    company_role: Optional[str] = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_backend_profile.py -v`
Expected: PASS

- [ ] **Step 5: Add the columns to the Postgres schema**

In `repositories/postgres/schema.sql`, replace the `users` table definition (currently lines 58-69):

```sql
CREATE TABLE IF NOT EXISTS users (
    user_id text PRIMARY KEY,
    email text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    user_type text NOT NULL CHECK (user_type IN ('candidate', 'recruiter')),
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz,
    -- Account-level profile fields (Profile page, 2026-09-06). Nullable so
    -- existing rows need no backfill. Candidate resume data (skills,
    -- experience, education) is intentionally NOT here - see users.py /
    -- CandidateRecord for why.
    full_name text,
    phone text,
    location text,
    headline text,
    bio text,
    avatar_url text,
    -- Recruiter-only; NULL for candidate accounts.
    company_name text,
    company_website text,
    company_role text
);
```

This is a plain `ALTER`-free `CREATE TABLE IF NOT EXISTS` change; it only takes effect for a fresh database. Note in the commit message (Step 6) that an already-provisioned Postgres instance needs the equivalent `ALTER TABLE users ADD COLUMN IF NOT EXISTS ...` run by hand — there is no migration runner in this codebase to add that to (schema.sql is applied once at first startup, per `core/lifespan.py`'s `_initialize_database_schema`).

- [ ] **Step 6: Commit**

```bash
git add repositories/interfaces.py repositories/postgres/schema.sql tests/test_backend_profile.py
git commit -m "feat(profile): add account-level profile fields to UserRecord and users table"
```

---

## Task 2: Persist the new fields in `PostgresUserRepository`

**Files:**
- Modify: `repositories/postgres/user_repository.py`
- Test: `tests/test_backend_profile.py`

**Interfaces:**
- Consumes: `UserRecord` fields from Task 1.
- Produces: `PostgresUserRepository.save()` persists and returns all profile fields; `_user_record_from_row` reads them back.

Postgres round-trip tests need a running database and are skipped in this environment the same way other Postgres-only paths in this codebase are exercised only in a deployed environment — this task changes the implementation and is verified by code review plus the existing `InMemoryUserRepository` path (Task 1's test already proves `UserRecord` itself is correct; `InMemoryUserRepository.save()` needs no code change since it stores the record as-is). No new automated test is added for the Postgres file itself; instead, this task's "test" step is running the full non-Postgres suite to prove nothing else broke.

- [ ] **Step 1: Update `_user_record_from_row` and `save()` in `repositories/postgres/user_repository.py`**

Replace `_user_record_from_row` (currently lines 21-30):

```python
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
        full_name=row["full_name"],
        phone=row["phone"],
        location=row["location"],
        headline=row["headline"],
        bio=row["bio"],
        avatar_url=row["avatar_url"],
        company_name=row["company_name"],
        company_website=row["company_website"],
        company_role=row["company_role"],
    )
```

Replace the `save()` method's SQL (currently lines 42-70):

```python
    async def save(self, record: UserRecord) -> UserRecord:
        """Insert or update by user_id. Idempotent, preserves created_at.

        Stores email in lowercase for case-insensitive lookups. Sets
        updated_at to the current timestamp on every write. Writes every
        profile field on the record, so a caller must pass the FULL desired
        state (fetch, `model_copy(update=...)`, then save - see
        `AuthService.update_profile`) rather than a sparse patch.
        """
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users
                    (user_id, email, password_hash, user_type, is_active, created_at,
                     updated_at, full_name, phone, location, headline, bio, avatar_url,
                     company_name, company_website, company_role)
                VALUES ($1, $2, $3, $4, $5, $6, now(), $7, $8, $9, $10, $11, $12, $13, $14, $15)
                ON CONFLICT (user_id) DO UPDATE
                    SET email = EXCLUDED.email,
                        password_hash = EXCLUDED.password_hash,
                        user_type = EXCLUDED.user_type,
                        is_active = EXCLUDED.is_active,
                        updated_at = now(),
                        full_name = EXCLUDED.full_name,
                        phone = EXCLUDED.phone,
                        location = EXCLUDED.location,
                        headline = EXCLUDED.headline,
                        bio = EXCLUDED.bio,
                        avatar_url = EXCLUDED.avatar_url,
                        company_name = EXCLUDED.company_name,
                        company_website = EXCLUDED.company_website,
                        company_role = EXCLUDED.company_role
                RETURNING user_id, email, password_hash, user_type, is_active, created_at,
                          updated_at, full_name, phone, location, headline, bio, avatar_url,
                          company_name, company_website, company_role
                """,
                record.user_id,
                record.email.lower(),
                record.password_hash,
                record.user_type,
                record.is_active,
                record.created_at,
                record.full_name,
                record.phone,
                record.location,
                record.headline,
                record.bio,
                record.avatar_url,
                record.company_name,
                record.company_website,
                record.company_role,
            )
        return _user_record_from_row(row)
```

`get_by_email` and `get_by_id` need no change — both already do `SELECT *` and pass the row to `_user_record_from_row`, which now reads the new columns.

- [ ] **Step 2: Run the full non-Postgres suite to confirm nothing broke**

Run: `pytest tests/ -v -k "not postgres"`
Expected: PASS (this file has no Postgres-backed tests today; this step is a regression check on everything else)

- [ ] **Step 3: Commit**

```bash
git add repositories/postgres/user_repository.py
git commit -m "feat(profile): persist profile fields in PostgresUserRepository"
```

---

## Task 3: `AuthService.get_profile` / `update_profile` / `change_password`

**Files:**
- Modify: `services/auth_service.py`
- Test: `tests/test_backend_profile.py`

**Interfaces:**
- Consumes: `UserRecord` (Task 1), `UserRepository.get_by_id`/`save` (existing, unchanged signatures), `core.errors.NotFoundError`, `core.errors.BadRequestError`, `core.errors.UnauthorizedError`.
- Produces (used by Task 4's routes):
  - `AuthService.get_profile(user_id: str) -> UserRecord` — raises `NotFoundError` if no such user.
  - `AuthService.update_profile(user_id: str, updates: dict) -> UserRecord` — `updates` is the result of the request model's `model_dump(exclude_unset=True)`; raises `NotFoundError` if no such user, `BadRequestError` if a candidate account supplies any of `company_name`/`company_website`/`company_role`.
  - `AuthService.change_password(user_id: str, current_password: str, new_password: str) -> None` — raises `NotFoundError` if no such user, `UnauthorizedError` if `current_password` does not match.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backend_profile.py`:

```python
# ---------------------------------------------------------------------------
# AuthService.get_profile / update_profile / change_password
# ---------------------------------------------------------------------------

from core.errors import BadRequestError, NotFoundError, UnauthorizedError
from core.config import AppSettings


@pytest.fixture
def user_repo():
    return InMemoryUserRepository()


@pytest.fixture
def auth_service(user_repo):
    return AuthService(user_repository=user_repo, settings=AppSettings())


class TestAuthServiceProfile:
    async def _signed_up_user(self, service, user_type="candidate"):
        user, _access, _refresh = await service.signup(
            email=f"{user_type}@example.com", password="password123", user_type=user_type
        )
        return user

    @pytest.mark.asyncio
    async def test_get_profile_returns_the_stored_record(self, auth_service):
        user = await self._signed_up_user(auth_service)
        fetched = await auth_service.get_profile(user.user_id)
        assert fetched.user_id == user.user_id
        assert fetched.full_name is None

    @pytest.mark.asyncio
    async def test_get_profile_unknown_user_is_not_found(self, auth_service):
        with pytest.raises(NotFoundError):
            await auth_service.get_profile("user_does_not_exist")

    @pytest.mark.asyncio
    async def test_update_profile_applies_only_supplied_fields(self, auth_service):
        user = await self._signed_up_user(auth_service)
        updated = await auth_service.update_profile(
            user.user_id, {"full_name": "Jane Doe", "location": "Remote"}
        )
        assert updated.full_name == "Jane Doe"
        assert updated.location == "Remote"
        assert updated.headline is None  # untouched, not cleared

        updated_again = await auth_service.update_profile(user.user_id, {"headline": "Engineer"})
        assert updated_again.full_name == "Jane Doe"  # still there
        assert updated_again.headline == "Engineer"

    @pytest.mark.asyncio
    async def test_update_profile_explicit_null_clears_a_field(self, auth_service):
        user = await self._signed_up_user(auth_service)
        await auth_service.update_profile(user.user_id, {"bio": "Hello"})
        cleared = await auth_service.update_profile(user.user_id, {"bio": None})
        assert cleared.bio is None

    @pytest.mark.asyncio
    async def test_update_profile_cannot_touch_account_fields(self, auth_service):
        """email/user_type/is_active/password_hash are not in the request
        model at all (Task 4), but this pins the service layer too: even a
        caller that assembled the dict directly cannot use these keys to
        change protected fields."""
        user = await self._signed_up_user(auth_service)
        updated = await auth_service.update_profile(
            user.user_id, {"email": "changed@example.com", "full_name": "Jane"}
        )
        assert updated.email == user.email  # unchanged
        assert updated.full_name == "Jane"

    @pytest.mark.asyncio
    async def test_update_profile_rejects_recruiter_fields_from_a_candidate(self, auth_service):
        user = await self._signed_up_user(auth_service, user_type="candidate")
        with pytest.raises(BadRequestError):
            await auth_service.update_profile(user.user_id, {"company_name": "Acme"})

    @pytest.mark.asyncio
    async def test_update_profile_allows_recruiter_fields_for_a_recruiter(self, auth_service):
        user = await self._signed_up_user(auth_service, user_type="recruiter")
        updated = await auth_service.update_profile(user.user_id, {"company_name": "Acme"})
        assert updated.company_name == "Acme"

    @pytest.mark.asyncio
    async def test_update_profile_unknown_user_is_not_found(self, auth_service):
        with pytest.raises(NotFoundError):
            await auth_service.update_profile("user_does_not_exist", {"full_name": "X"})

    @pytest.mark.asyncio
    async def test_change_password_succeeds_with_correct_current_password(self, auth_service):
        user = await self._signed_up_user(auth_service)
        await auth_service.change_password(user.user_id, "password123", "newpassword456")
        # Login with the new password now works; the old one doesn't.
        logged_in, _, _ = await auth_service.login(user.email, "newpassword456")
        assert logged_in.user_id == user.user_id
        with pytest.raises(UnauthorizedError):
            await auth_service.login(user.email, "password123")

    @pytest.mark.asyncio
    async def test_change_password_rejects_wrong_current_password(self, auth_service):
        user = await self._signed_up_user(auth_service)
        with pytest.raises(UnauthorizedError):
            await auth_service.change_password(user.user_id, "wrongpassword", "newpassword456")

    @pytest.mark.asyncio
    async def test_change_password_unknown_user_is_not_found(self, auth_service):
        with pytest.raises(NotFoundError):
            await auth_service.change_password("user_does_not_exist", "a", "newpassword456")
```

(This plan assumes `pytest-asyncio` with `asyncio_mode = auto` or an existing `@pytest.mark.asyncio` convention — confirm against `pytest.ini`/`pyproject.toml` in Step 2 below and match whichever the rest of the suite uses; every other async test file in `tests/` already runs successfully, so mirror its marker/config exactly rather than introducing a new one.)

- [ ] **Step 2: Check the existing async test convention, then run to verify failure**

Run: `grep -n "asyncio_mode\|asyncio" pytest.ini pyproject.toml setup.cfg 2>/dev/null` to see the project's convention; adjust the test file's markers/imports to match (e.g. drop `@pytest.mark.asyncio` if `asyncio_mode = auto` is already set).

Run: `pytest tests/test_backend_profile.py -v`
Expected: FAIL — `AttributeError: 'AuthService' object has no attribute 'get_profile'`

- [ ] **Step 3: Implement the three methods on `AuthService`**

In `services/auth_service.py`, add the import and the methods. Update the imports at the top:

```python
from core.errors import BadRequestError, ConflictError, NotFoundError, UnauthorizedError
```

Add a module-level constant near the top of the file (after the imports, before `class AuthService`):

```python
# Fields on UserRecord that only make sense for a recruiter account. A
# candidate account supplying any of these to update_profile is a client
# error, not a silent no-op - the caller should learn its request doesn't
# match its own account type.
_RECRUITER_ONLY_PROFILE_FIELDS = {"company_name", "company_website", "company_role"}

# Fields update_profile will never touch, even if a caller's dict happens to
# contain them - authentication/authorization state is not "profile".
_PROTECTED_ACCOUNT_FIELDS = {"user_id", "email", "password_hash", "user_type", "is_active"}
```

Add the three methods to `AuthService`, after `login` and before `refresh_access_token`:

```python
    async def get_profile(self, user_id: str) -> UserRecord:
        """The full stored profile for one user.

        Raises:
            NotFoundError: If no user with this id exists.
        """
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise NotFoundError(
                "No account found.",
                internal_detail=f"user_id={user_id!r} not found",
            )
        return user

    async def update_profile(self, user_id: str, updates: dict) -> UserRecord:
        """Apply a partial update to one user's profile fields.

        `updates` should come from a request model's `model_dump(exclude_unset=True)`
        so that an omitted field is left alone and an explicit `null` clears
        it - both are ordinary dict operations once collapsed to a dict,
        which is why this method takes a plain dict rather than the request
        schema itself (keeping schemas/auth.py's shape out of the service
        layer).

        Any key in `_PROTECTED_ACCOUNT_FIELDS` is silently ignored rather
        than applied - `schemas.auth.UpdateProfileRequest` (Task 4) never
        includes them, so this only matters for a caller that assembled the
        dict by hand (as the tests above do to pin the behaviour).

        Raises:
            NotFoundError: If no user with this id exists.
            BadRequestError: If a candidate account's update includes any
                recruiter-only field.
        """
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise NotFoundError(
                "No account found.",
                internal_detail=f"user_id={user_id!r} not found",
            )

        safe_updates = {k: v for k, v in updates.items() if k not in _PROTECTED_ACCOUNT_FIELDS}

        if user.user_type != "recruiter":
            offending = _RECRUITER_ONLY_PROFILE_FIELDS & safe_updates.keys()
            if offending:
                raise BadRequestError(
                    f"These fields are only valid for a recruiter account: {sorted(offending)}",
                    internal_detail=(
                        f"user {user_id!r} (user_type={user.user_type!r}) attempted to set "
                        f"recruiter-only fields: {sorted(offending)}"
                    ),
                )

        updated = user.model_copy(update=safe_updates)
        stored = await self._users.save(updated)

        logger.info(
            "profile updated",
            extra=log_context(event="profile_updated", user_id=user_id, fields=sorted(safe_updates)),
        )
        return stored

    async def change_password(self, user_id: str, current_password: str, new_password: str) -> None:
        """Verify the current password and replace it with a new hash.

        Raises:
            NotFoundError: If no user with this id exists.
            UnauthorizedError: If `current_password` does not match.
        """
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise NotFoundError(
                "No account found.",
                internal_detail=f"user_id={user_id!r} not found",
            )

        if not self._verify_password(current_password, user.password_hash):
            logger.warning(
                "password change attempt with wrong current password",
                extra=log_context(event="password_change_invalid_current", user_id=user_id),
            )
            raise UnauthorizedError(
                "Current password is incorrect.",
                internal_detail=f"password verification failed for user {user_id!r}",
            )

        updated = user.model_copy(update={"password_hash": self._hash_password(new_password)})
        await self._users.save(updated)

        logger.info(
            "password changed",
            extra=log_context(event="password_changed", user_id=user_id),
        )
```

Update the module docstring's method list at the top of the file to mention the three new methods, matching the existing style:

```python
"""
Authentication service implementation.

Handles user signup, login, token generation and validation. Integrates with
UserRepository for persistence and AppSettings for configuration.

Methods provided:
  - signup(email, password, user_type): Create a new user account and generate tokens.
  - login(email, password): Authenticate user and return access/refresh tokens.
  - refresh_access_token(refresh_token): Generate a new access token from refresh token.
  - verify_access_token(token): Decode and validate JWT access token, return Principal.
  - get_profile(user_id): Fetch the full stored profile for one user.
  - update_profile(user_id, updates): Apply a partial profile update.
  - change_password(user_id, current_password, new_password): Verify and replace a password.
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_backend_profile.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/auth_service.py tests/test_backend_profile.py
git commit -m "feat(profile): add get_profile/update_profile/change_password to AuthService"
```

---

## Task 4: `schemas/auth.py` request/response models

**Files:**
- Modify: `schemas/auth.py`
- Test: `tests/test_backend_profile.py`

**Interfaces:**
- Consumes: `UserRecord` (Task 1).
- Produces (used by Task 5's routes):
  - `UserProfileResponse` — Pydantic model with a `from_record(cls, record: UserRecord) -> UserProfileResponse` classmethod, matching the `CandidateResponse.from_record` pattern (`api/models_candidates.py:44`).
  - `UpdateProfileRequest` — every field `Optional`, no `email`/`user_type`/`is_active`/`password_hash` fields at all.
  - `ChangePasswordRequest` — `current_password: str`, `new_password: str = Field(min_length=8, ...)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backend_profile.py`:

```python
# ---------------------------------------------------------------------------
# schemas/auth.py: UserProfileResponse / UpdateProfileRequest / ChangePasswordRequest
# ---------------------------------------------------------------------------

from schemas.auth import ChangePasswordRequest, UpdateProfileRequest, UserProfileResponse


class TestProfileSchemas:
    def test_user_profile_response_from_record_carries_no_password_hash(self):
        record = UserRecord(
            user_id="user_1",
            email="a@example.com",
            password_hash="secret-hash",
            user_type="candidate",
            full_name="Jane Doe",
        )
        response = UserProfileResponse.from_record(record)
        assert response.full_name == "Jane Doe"
        assert response.email == "a@example.com"
        assert "password_hash" not in response.model_dump()
        assert "secret-hash" not in response.model_dump_json()

    def test_update_profile_request_has_no_protected_fields(self):
        field_names = set(UpdateProfileRequest.model_fields.keys())
        assert field_names.isdisjoint({"email", "user_type", "is_active", "password_hash", "user_id"})

    def test_update_profile_request_exclude_unset_only_carries_supplied_fields(self):
        request = UpdateProfileRequest(full_name="Jane")
        assert request.model_dump(exclude_unset=True) == {"full_name": "Jane"}

    def test_change_password_request_rejects_short_new_password(self):
        with pytest.raises(Exception):
            ChangePasswordRequest(current_password="old12345", new_password="short")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_profile.py -v`
Expected: FAIL — `ImportError: cannot import name 'UserProfileResponse' from 'schemas.auth'`

- [ ] **Step 3: Add the schemas**

In `schemas/auth.py`, append after `UserResponse`:

```python
class UserProfileResponse(BaseModel):
    """Full self-service profile view, returned by GET/PATCH /auth/me.

    Deliberately excludes password_hash - UserResponse (above) stays the
    minimal shape used in auth flows; this is the richer shape for the
    profile page. Never includes candidate resume data (skills, experience,
    education): that comes from GET /candidates/me and is read-only here.
    """

    user_id: str
    email: str
    user_type: str
    created_at: datetime

    full_name: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    headline: Optional[str] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None

    company_name: Optional[str] = None
    company_website: Optional[str] = None
    company_role: Optional[str] = None

    @classmethod
    def from_record(cls, record: "UserRecord") -> "UserProfileResponse":
        return cls(
            user_id=record.user_id,
            email=record.email,
            user_type=record.user_type,
            created_at=record.created_at,
            full_name=record.full_name,
            phone=record.phone,
            location=record.location,
            headline=record.headline,
            bio=record.bio,
            avatar_url=record.avatar_url,
            company_name=record.company_name,
            company_website=record.company_website,
            company_role=record.company_role,
        )


class UpdateProfileRequest(BaseModel):
    """PATCH /auth/me request body.

    Every field is optional so a caller can send only what changed.
    `model_dump(exclude_unset=True)` (used by the route) distinguishes an
    omitted field (left alone) from an explicit `null` (clears it) - a
    plain default-based dump could not tell these apart.

    No email/user_type/is_active/password_hash field exists here at all,
    so a client cannot even attempt to set them through this endpoint.
    """

    full_name: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    headline: Optional[str] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None

    company_name: Optional[str] = None
    company_website: Optional[str] = None
    company_role: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    """POST /auth/me/password request body."""

    current_password: str
    new_password: str = Field(min_length=8, description="Password must be at least 8 characters")
```

Add the needed imports at the top of `schemas/auth.py`:

```python
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field
```

(`BaseModel`, `EmailStr`, `Field` already exist there; add `datetime` and `Optional` if not already imported — check the file's current imports before adding to avoid a duplicate.)

Add a forward-reference-safe import for `UserRecord` used only in the type hint — since `repositories/interfaces.py` does not import `schemas/auth.py`, there's no circular import risk in importing it directly instead of using a string forward reference:

```python
from repositories.interfaces import UserRecord
```

Replace the `"UserRecord"` string annotations in `from_record` above with the plain `UserRecord` type once this import is in place.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_backend_profile.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schemas/auth.py tests/test_backend_profile.py
git commit -m "feat(profile): add UserProfileResponse/UpdateProfileRequest/ChangePasswordRequest schemas"
```

---

## Task 5: `GET/PATCH /auth/me` and `POST /auth/me/password` routes

**Files:**
- Modify: `api/routes/auth.py`
- Test: `tests/test_backend_profile.py`

**Interfaces:**
- Consumes: `AuthService.get_profile`/`update_profile`/`change_password` (Task 3), `UserProfileResponse`/`UpdateProfileRequest`/`ChangePasswordRequest` (Task 4), `require_authenticated` (`core/security.py:227`), `Principal` (`core/security.py`).
- Produces: the three HTTP endpoints later consumed by the frontend (Task 8).

This task's tests need a **real authenticated principal** tied to a specific signed-up user (not the always-anonymous default `client` fixture other test files use), because verifying per-user isolation and password changes requires knowing which user is calling. Build a dedicated fixture that wires `JWTAuthProvider` onto an in-memory container with `AUTH_ENABLED=true` — this only works because a real `AuthProvider` is installed, satisfying `validate_startup_configuration`'s check (`core/lifespan.py:159`) that a claimed-enabled auth cannot run on `AnonymousAuthProvider`.

- [ ] **Step 1: Write the failing tests, including the authenticated-app fixture**

Append to `tests/test_backend_profile.py`:

```python
# ---------------------------------------------------------------------------
# HTTP layer: GET/PATCH /auth/me, POST /auth/me/password
#
# Uses a real JWTAuthProvider over an in-memory UserRepository so these
# tests can prove per-user identity end to end (which user a token
# resolves to matters here, unlike the anonymous-principal tests
# elsewhere in this codebase). AUTH_ENABLED=true is safe with an in-memory
# backend as long as a real AuthProvider is installed - see
# core/lifespan.py's validate_startup_configuration.
# ---------------------------------------------------------------------------

def _authenticated_app():
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
        evaluation_dispatcher=AsyncTaskEvaluationDispatcher(),
        auth_provider=JWTAuthProvider(auth_service),
        persistence_is_ephemeral=True,
    )
    app = create_app(settings)
    # Bypassing the lifespan's own build_default_container (core/lifespan.py)
    # is deliberate: that factory has no branch for "in-memory + real auth
    # provider", and adding one there would be a production wiring change
    # for a test-only need. TestClient below is used WITHOUT the `with`
    # context manager, so lifespan startup never runs and never overwrites
    # this container.
    app.state.container = container
    return app, auth_service


@pytest.fixture
def authenticated_client():
    app, auth_service = _authenticated_app()
    client = TestClient(app)
    return client, auth_service


def _signup_and_get_token(client, email="candidate@example.com", user_type="candidate"):
    response = client.post(
        "/auth/signup",
        json={"email": email, "password": "password123", "user_type": user_type},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["user"]["user_id"], body["access_token"]


class TestGetMe:
    def test_returns_the_caller_own_profile(self, authenticated_client):
        client, _ = authenticated_client
        user_id, token = _signup_and_get_token(client)
        response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == user_id
        assert body["email"] == "candidate@example.com"
        assert body["user_type"] == "candidate"
        assert "password_hash" not in body

    def test_requires_authentication(self, authenticated_client):
        client, _ = authenticated_client
        response = client.get("/auth/me")
        assert response.status_code == 401


class TestPatchMe:
    def test_updates_supplied_fields_only(self, authenticated_client):
        client, _ = authenticated_client
        _user_id, token = _signup_and_get_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        response = client.patch("/auth/me", json={"full_name": "Jane Doe"}, headers=headers)
        assert response.status_code == 200
        assert response.json()["full_name"] == "Jane Doe"

        response = client.patch("/auth/me", json={"location": "Remote"}, headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["full_name"] == "Jane Doe"  # still set
        assert body["location"] == "Remote"

    def test_cannot_change_email_or_user_type(self, authenticated_client):
        client, _ = authenticated_client
        _user_id, token = _signup_and_get_token(client)
        headers = {"Authorization": f"Bearer {token}"}
        response = client.patch(
            "/auth/me",
            json={"email": "new@example.com", "user_type": "recruiter"},
            headers=headers,
        )
        # email/user_type are not fields on UpdateProfileRequest, so FastAPI
        # ignores them rather than erroring - the response proves neither
        # took effect.
        assert response.status_code == 200
        body = response.json()
        assert body["email"] == "candidate@example.com"
        assert body["user_type"] == "candidate"

    def test_rejects_recruiter_fields_from_a_candidate(self, authenticated_client):
        client, _ = authenticated_client
        _user_id, token = _signup_and_get_token(client, user_type="candidate")
        headers = {"Authorization": f"Bearer {token}"}
        response = client.patch("/auth/me", json={"company_name": "Acme"}, headers=headers)
        assert response.status_code == 400

    def test_allows_recruiter_fields_for_a_recruiter(self, authenticated_client):
        client, _ = authenticated_client
        _user_id, token = _signup_and_get_token(
            client, email="recruiter@example.com", user_type="recruiter"
        )
        headers = {"Authorization": f"Bearer {token}"}
        response = client.patch("/auth/me", json={"company_name": "Acme"}, headers=headers)
        assert response.status_code == 200
        assert response.json()["company_name"] == "Acme"

    def test_requires_authentication(self, authenticated_client):
        client, _ = authenticated_client
        response = client.patch("/auth/me", json={"full_name": "X"})
        assert response.status_code == 401


class TestChangePassword:
    def test_succeeds_with_correct_current_password(self, authenticated_client):
        client, _ = authenticated_client
        _user_id, token = _signup_and_get_token(client)
        headers = {"Authorization": f"Bearer {token}"}
        response = client.post(
            "/auth/me/password",
            json={"current_password": "password123", "new_password": "newpassword456"},
            headers=headers,
        )
        assert response.status_code == 204

        # Old password no longer works; new one does.
        login_old = client.post(
            "/auth/login", json={"email": "candidate@example.com", "password": "password123"}
        )
        assert login_old.status_code == 401
        login_new = client.post(
            "/auth/login", json={"email": "candidate@example.com", "password": "newpassword456"}
        )
        assert login_new.status_code == 200

    def test_rejects_wrong_current_password(self, authenticated_client):
        client, _ = authenticated_client
        _user_id, token = _signup_and_get_token(client)
        headers = {"Authorization": f"Bearer {token}"}
        response = client.post(
            "/auth/me/password",
            json={"current_password": "wrongpassword", "new_password": "newpassword456"},
            headers=headers,
        )
        assert response.status_code == 401

    def test_requires_authentication(self, authenticated_client):
        client, _ = authenticated_client
        response = client.post(
            "/auth/me/password",
            json={"current_password": "a", "new_password": "newpassword456"},
        )
        assert response.status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_backend_profile.py -v`
Expected: FAIL — `404 Not Found` for `/auth/me` (route doesn't exist yet)

- [ ] **Step 3: Add the routes**

In `api/routes/auth.py`, update the imports:

```python
from fastapi import APIRouter, Depends, status

from core.config import AppSettings, get_settings
from core.dependencies import get_user_repository
from core.logging import get_logger
from core.security import Principal, require_authenticated
from repositories.interfaces import UserRepository
from schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    RefreshTokenRequest,
    RefreshTokenResponse,
    SignupRequest,
    UpdateProfileRequest,
    UserProfileResponse,
    UserResponse,
)
from services.auth_service import AuthService
```

Update the module docstring's endpoint list:

```python
"""
Authentication API routes.

Provides endpoints for user signup, login, token refresh, and self-service
profile management. Uses FastAPI dependency injection to wire
UserRepository and AppSettings.

Endpoints:
  - POST /auth/signup: Create new user account, return tokens + user
  - POST /auth/login: Authenticate user, return tokens + user
  - POST /auth/refresh: Exchange refresh token for new access token
  - GET /auth/me: The authenticated caller's own profile
  - PATCH /auth/me: Update the authenticated caller's own profile
  - POST /auth/me/password: Change the authenticated caller's own password
"""
```

Add the three routes at the end of the file, before `__all__ = ["router"]`:

```python
@router.get("/me", response_model=UserProfileResponse)
async def get_my_profile(
    service: AuthService = Depends(get_auth_service),
    principal: Principal = Depends(require_authenticated),
) -> UserProfileResponse:
    """GET /auth/me - the authenticated caller's own full profile.

    Errors:
      - 401 Unauthorized: no valid credential presented
      - 404 not_found: the token's subject no longer has an account
        (should not happen in practice - the account that minted the token
        would have to have been deleted since)
    """
    user = await service.get_profile(principal.subject_id)
    return UserProfileResponse.from_record(user)


@router.patch("/me", response_model=UserProfileResponse)
async def update_my_profile(
    request: UpdateProfileRequest,
    service: AuthService = Depends(get_auth_service),
    principal: Principal = Depends(require_authenticated),
) -> UserProfileResponse:
    """PATCH /auth/me - partially update the authenticated caller's own profile.

    Only fields present in the request body are changed; an omitted field
    is left as-is, an explicit `null` clears it. email/user_type/is_active/
    password_hash cannot be set through this endpoint - they are not fields
    on `UpdateProfileRequest` at all.

    Errors:
      - 401 Unauthorized: no valid credential presented
      - 400 invalid_request: a candidate account supplied a recruiter-only
        field (company_name/company_website/company_role)
      - 404 not_found: the token's subject no longer has an account
    """
    updates = request.model_dump(exclude_unset=True)
    user = await service.update_profile(principal.subject_id, updates)
    return UserProfileResponse.from_record(user)


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_my_password(
    request: ChangePasswordRequest,
    service: AuthService = Depends(get_auth_service),
    principal: Principal = Depends(require_authenticated),
) -> None:
    """POST /auth/me/password - change the authenticated caller's own password.

    Errors:
      - 401 Unauthorized: no valid credential presented, OR current_password
        does not match
      - 404 not_found: the token's subject no longer has an account
      - 422 Unprocessable Entity: new_password shorter than 8 characters
    """
    await service.change_password(
        principal.subject_id, request.current_password, request.new_password
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_backend_profile.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `pytest tests/ -v`
Expected: PASS (all prior tests remain green; the existing default `client` fixture in other files is untouched since `AUTH_ENABLED` still defaults to `false` there)

- [ ] **Step 6: Commit**

```bash
git add api/routes/auth.py tests/test_backend_profile.py
git commit -m "feat(profile): add GET/PATCH /auth/me and POST /auth/me/password endpoints"
```

---

## Task 6: Navbar dropdown

**Files:**
- Modify: `pages/js/app.js:231-263` (`renderNavbar`)
- Modify: `pages/css/style.css` (append new rules)

**Interfaces:**
- Consumes: `getCurrentUser()`, `logoutUser()` (existing, `pages/js/app.js`).
- Produces: a `.nav-menu`/`.nav-menu-trigger` DOM structure and a global `toggleNavMenu(event)` function, consumed by no other task but exercised manually (Step 4).

This is frontend-only and has no `pytest` coverage in this codebase's test suite (no JS test runner is configured anywhere in `tests/` or `package.json`), so verification here is a manual browser check instead of an automated test — matching how every other page in `pages/` is verified today (there are no existing JS tests to follow as precedent).

- [ ] **Step 1: Replace `renderNavbar`'s `.nav-right` block**

In `pages/js/app.js`, replace the `renderNavbar` function (currently lines 231-263):

```javascript
function renderNavbar(activePage = '') {
  const user = getCurrentUser();
  const navContainer = document.getElementById('navbarContainer');
  if (!navContainer) return;

  const isRecruiter = user && user.role === 'recruiter';
  const dashboardHref = isRecruiter ? 'recruiter.html' : 'candidate.html';

  navContainer.innerHTML = `
    <header class="navbar">
      <div class="brand-logo" onclick="window.location.href='index.html'">
        <span class="brand-dot"></span>OpenHire
      </div>
      
      <nav class="nav-links">
        <a href="index.html">Overview</a>
        <a href="${dashboardHref}" class="${activePage === 'dashboard' ? 'active' : ''}">Dashboard</a>
        ${isRecruiter ? `<a href="create-job.html" class="${activePage === 'create-job' ? 'active' : ''}">+ Job</a>` : ''}
        ${isRecruiter ? `<a href="screening.html" class="${activePage === 'screening' ? 'active' : ''}">Screening</a>` : ''}
        <a href="leaderboard.html" class="${activePage === 'leaderboard' ? 'active' : ''}">Leaderboard</a>
        <a href="openbox.html" class="${activePage === 'openbox' ? 'active' : ''}">OpenBox</a>
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

/** Toggles the top-right nav dropdown. Closes on an outside click or
 * Escape, and re-attaches those listeners each time it opens rather than
 * once at page load, since renderNavbar rebuilds this DOM from scratch on
 * every call (e.g. after setCurrentUser). */
function toggleNavMenu(event) {
  event.stopPropagation();
  const dropdown = document.getElementById('navMenuDropdown');
  if (!dropdown) return;

  const opening = dropdown.hidden;
  dropdown.hidden = !opening;
  if (!opening) return;

  const closeOnOutsideClick = (e) => {
    if (!dropdown.contains(e.target)) {
      dropdown.hidden = true;
      document.removeEventListener('click', closeOnOutsideClick);
      document.removeEventListener('keydown', closeOnEscape);
    }
  };
  const closeOnEscape = (e) => {
    if (e.key === 'Escape') {
      dropdown.hidden = true;
      document.removeEventListener('click', closeOnOutsideClick);
      document.removeEventListener('keydown', closeOnEscape);
    }
  };
  document.addEventListener('click', closeOnOutsideClick);
  document.addEventListener('keydown', closeOnEscape);
}
```

- [ ] **Step 2: Add the dropdown CSS**

Append to `pages/css/style.css`, after the existing `.user-badge .role-tag` rule (around line 182):

```css
.nav-menu { position: relative; }

.nav-menu-trigger {
  cursor: pointer;
  font-family: var(--font-mono);
}

.nav-menu-dropdown {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  min-width: 160px;
  background: var(--surface-elevated);
  border: 1px solid var(--surface-border);
  border-radius: var(--radius-sm);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
  padding: 0.35rem;
  z-index: 200;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.nav-menu-dropdown a,
.nav-menu-item-btn {
  display: block;
  width: 100%;
  text-align: left;
  padding: 0.5rem 0.65rem;
  border-radius: 4px;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-main);
  background: none;
  border: none;
  cursor: pointer;
  text-decoration: none;
}

.nav-menu-dropdown a:hover,
.nav-menu-item-btn:hover {
  background: var(--surface-hover);
}

.nav-menu-divider {
  height: 1px;
  background: var(--surface-border);
  margin: 0.25rem 0;
}

.profile-avatar {
  width: 64px;
  height: 64px;
  border-radius: 50%;
  background: var(--primary-subtle);
  color: var(--primary);
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono);
  font-size: 22px;
  font-weight: 700;
  flex-shrink: 0;
}

.profile-header-row { display: flex; align-items: center; gap: 1rem; }

.chip-row { display: flex; flex-wrap: wrap; gap: 0.4rem; }

.chip {
  background: var(--surface);
  border: 1px solid var(--surface-border);
  border-radius: 999px;
  padding: 0.2rem 0.65rem;
  font-size: 11.5px;
  font-family: var(--font-mono);
  color: var(--text-muted);
}

.form-status { font-size: 12px; margin-top: 0.5rem; min-height: 1em; }
.form-status.success { color: var(--success-text); }
.form-status.error { color: var(--danger-text); }
```

- [ ] **Step 3: Grep every page that includes `navbarContainer` to confirm none define a conflicting `toggleNavMenu` or `.nav-menu` already**

Run: `grep -rl "navbarContainer" pages/*.html | xargs grep -l "toggleNavMenu\|nav-menu"`
Expected: no output (no existing conflicts) — if there IS output, inspect that file before proceeding, since it means another page already defines something with this name.

- [ ] **Step 4: Manual verification**

Open any page with `navbarContainer` (e.g. `pages/candidate.html`) in a browser with a `openhire_user` value in `localStorage`, confirm: clicking the badge opens the dropdown, clicking outside closes it, `Escape` closes it, and "Sign out" still logs out.

- [ ] **Step 5: Commit**

```bash
git add pages/js/app.js pages/css/style.css
git commit -m "feat(profile): turn the navbar user badge into a Profile/API Keys/Sign out dropdown"
```

---

## Task 7: Frontend API helpers

**Files:**
- Modify: `pages/js/app.js`

**Interfaces:**
- Consumes: `apiRequest` (existing, `pages/js/app.js:37`).
- Produces (used by Task 8's `profile.html`):
  - `fetchMyProfile() -> Promise<object>` — GET `/auth/me`, returns the parsed `UserProfileResponse` body.
  - `updateMyProfile(fields: object) -> Promise<object>` — PATCH `/auth/me` with `fields` as the body, returns the updated profile.
  - `changeMyPassword(currentPassword: string, newPassword: string) -> Promise<void>` — POST `/auth/me/password`.

- [ ] **Step 1: Add the helpers**

In `pages/js/app.js`, add after `fetchApplicationsForCandidate` (currently around line 138):

```javascript
/** GET /auth/me - the signed-in user's full profile. */
async function fetchMyProfile() {
  return apiRequest('/auth/me');
}

/** PATCH /auth/me with a partial set of fields. Pass only the fields that
 * changed; omit a field entirely to leave it alone, or pass `null` to
 * clear it - both are handled by the backend's exclude_unset semantics
 * (schemas/auth.py:UpdateProfileRequest). */
async function updateMyProfile(fields) {
  return apiRequest('/auth/me', { method: 'PATCH', body: fields });
}

/** POST /auth/me/password. Resolves with no value on success (204); throws
 * (via apiRequest's existing error handling) with a human-readable message
 * on a wrong current password (401) or a too-short new password (422). */
async function changeMyPassword(currentPassword, newPassword) {
  return apiRequest('/auth/me/password', {
    method: 'POST',
    body: { current_password: currentPassword, new_password: newPassword },
  });
}
```

- [ ] **Step 2: Manual verification**

With the backend running locally (`uvicorn api.app:app --reload` or the project's usual run command) and a signed-in browser session, open the devtools console on any page and run:

```javascript
await fetchMyProfile()
```

Expected: the signed-in user's profile object, no thrown error.

- [ ] **Step 3: Commit**

```bash
git add pages/js/app.js
git commit -m "feat(profile): add fetchMyProfile/updateMyProfile/changeMyPassword frontend helpers"
```

---

## Task 8: `pages/profile.html`

**Files:**
- Create: `pages/profile.html`

**Interfaces:**
- Consumes: `renderNavbar` (Task 6), `fetchMyProfile`/`updateMyProfile`/`changeMyPassword` (Task 7), `getCurrentUser`/`logoutUser` (existing), `apiRequest` (existing, for the read-only candidate/recruiter sections), existing CSS classes (`.card`, `.form-group`, `.form-control`, `.btn-primary`, `.tag`) plus the new ones from Task 6 (`.profile-avatar`, `.chip`, `.form-status`).

- [ ] **Step 1: Write the page**

Create `pages/profile.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenHire — Profile</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="css/style.css">
</head>
<body>
<div id="navbarContainer"></div>

<main class="container">
  <div class="card-header" style="margin-bottom:1.5rem;">
    <div>
      <h2 class="card-title">Your Profile</h2>
      <p class="card-sub">Account details, role information, and security settings.</p>
    </div>
  </div>

  <div class="card">
    <div class="profile-header-row">
      <div class="profile-avatar" id="profileAvatar">?</div>
      <div>
        <h3 class="card-title" id="profileName" style="font-size:1.1rem;">—</h3>
        <p class="card-sub" id="profileEmail" style="margin:0.15rem 0;">—</p>
        <span class="tag" id="profileRoleTag">—</span>
        <span class="card-sub" id="profileMemberSince" style="margin-left:0.5rem;"></span>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-header">
      <h3 class="card-title" style="font-size:1.05rem;">Account Details</h3>
    </div>
    <form id="accountForm">
      <div class="form-group">
        <label class="form-label" for="fullName">Full name</label>
        <input class="form-control" id="fullName" name="full_name" type="text">
      </div>
      <div class="form-group">
        <label class="form-label" for="phone">Phone</label>
        <input class="form-control" id="phone" name="phone" type="text">
      </div>
      <div class="form-group">
        <label class="form-label" for="location">Location</label>
        <input class="form-control" id="location" name="location" type="text">
      </div>
      <div class="form-group">
        <label class="form-label" for="headline">Headline</label>
        <input class="form-control" id="headline" name="headline" type="text" placeholder="e.g. Senior Backend Engineer">
      </div>
      <div class="form-group">
        <label class="form-label" for="bio">Bio</label>
        <textarea class="form-control" id="bio" name="bio" rows="3"></textarea>
      </div>
      <button type="submit" class="btn-primary">Save changes</button>
      <div class="form-status" id="accountFormStatus"></div>
    </form>
  </div>

  <div class="card" id="roleCard" hidden>
    <!-- Populated by JS: recruiter company form, or candidate read-only resume summary. -->
  </div>

  <div class="card">
    <div class="card-header">
      <h3 class="card-title" style="font-size:1.05rem;">Security</h3>
    </div>
    <form id="passwordForm">
      <div class="form-group">
        <label class="form-label" for="currentPassword">Current password</label>
        <input class="form-control" id="currentPassword" name="current_password" type="password" required>
      </div>
      <div class="form-group">
        <label class="form-label" for="newPassword">New password</label>
        <input class="form-control" id="newPassword" name="new_password" type="password" minlength="8" required>
      </div>
      <button type="submit" class="btn-primary">Change password</button>
      <div class="form-status" id="passwordFormStatus"></div>
    </form>
  </div>

  <div class="card" id="api-keys">
    <div class="card-header">
      <h3 class="card-title" style="font-size:1.05rem;">API Keys</h3>
      <span class="tag">Coming soon</span>
    </div>
    <p class="card-sub">
      Bring your own API key to run OpenHire's pipeline against the provider
      of your choice (OpenAI, Groq, Gemini, NVIDIA NIM). Key management
      lands in a follow-up release.
    </p>
    <button class="btn-outline" disabled>Manage keys</button>
  </div>
</main>

<script src="js/app.js"></script>
<script>
let currentProfile = null;

function initials(name, email) {
  const source = (name && name.trim()) || (email || '').split('@')[0] || '?';
  const parts = source.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function formatMemberSince(createdAtIso) {
  try {
    const date = new Date(createdAtIso);
    return `Member since ${date.toLocaleDateString(undefined, { year: 'numeric', month: 'long' })}`;
  } catch (e) {
    return '';
  }
}

function fillAccountForm(profile) {
  document.getElementById('fullName').value = profile.full_name || '';
  document.getElementById('phone').value = profile.phone || '';
  document.getElementById('location').value = profile.location || '';
  document.getElementById('headline').value = profile.headline || '';
  document.getElementById('bio').value = profile.bio || '';
}

function renderHeader(profile) {
  document.getElementById('profileAvatar').textContent = initials(profile.full_name, profile.email);
  document.getElementById('profileName').textContent = profile.full_name || profile.email.split('@')[0];
  document.getElementById('profileEmail').textContent = profile.email;
  document.getElementById('profileRoleTag').textContent = profile.user_type;
  document.getElementById('profileMemberSince').textContent = formatMemberSince(profile.created_at);
}

function renderRecruiterCard(profile) {
  const card = document.getElementById('roleCard');
  card.hidden = false;
  card.innerHTML = `
    <div class="card-header">
      <h3 class="card-title" style="font-size:1.05rem;">Company</h3>
    </div>
    <form id="companyForm">
      <div class="form-group">
        <label class="form-label" for="companyName">Company name</label>
        <input class="form-control" id="companyName" name="company_name" type="text" value="${(profile.company_name || '').replace(/"/g, '&quot;')}">
      </div>
      <div class="form-group">
        <label class="form-label" for="companyWebsite">Company website</label>
        <input class="form-control" id="companyWebsite" name="company_website" type="text" value="${(profile.company_website || '').replace(/"/g, '&quot;')}">
      </div>
      <div class="form-group">
        <label class="form-label" for="companyRole">Your role at the company</label>
        <input class="form-control" id="companyRole" name="company_role" type="text" value="${(profile.company_role || '').replace(/"/g, '&quot;')}">
      </div>
      <button type="submit" class="btn-primary">Save company details</button>
      <div class="form-status" id="companyFormStatus"></div>
    </form>
  `;

  document.getElementById('companyForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const status = document.getElementById('companyFormStatus');
    status.textContent = '';
    status.className = 'form-status';
    try {
      const updated = await updateMyProfile({
        company_name: document.getElementById('companyName').value || null,
        company_website: document.getElementById('companyWebsite').value || null,
        company_role: document.getElementById('companyRole').value || null,
      });
      currentProfile = updated;
      status.textContent = 'Saved.';
      status.className = 'form-status success';
    } catch (err) {
      status.textContent = err.message || 'Could not save company details.';
      status.className = 'form-status error';
    }
  });
}

async function renderCandidateCard() {
  const card = document.getElementById('roleCard');
  card.hidden = false;
  card.innerHTML = `
    <div class="card-header">
      <h3 class="card-title" style="font-size:1.05rem;">Resume Summary</h3>
    </div>
    <p class="card-sub">Loading…</p>
  `;

  try {
    const candidate = await apiRequest('/candidates/me');
    const resume = candidate.resume || {};
    const skills = (resume.skills || []).concat(resume.technologies || []);
    const latestRole = (resume.work_experience || [])[0];
    const latestEducation = (resume.education || [])[0];

    card.innerHTML = `
      <div class="card-header">
        <h3 class="card-title" style="font-size:1.05rem;">Resume Summary</h3>
        <a href="apply.html" class="tag">Update via resume</a>
      </div>
      ${resume.total_experience_years != null ? `<p class="card-sub">${resume.total_experience_years} years of experience</p>` : ''}
      ${latestRole ? `<p class="card-sub">Most recent: ${latestRole.title || ''}${latestRole.company ? ' at ' + latestRole.company : ''}</p>` : ''}
      ${latestEducation ? `<p class="card-sub">Education: ${latestEducation.degree || ''}${latestEducation.institution ? ', ' + latestEducation.institution : ''}</p>` : ''}
      ${skills.length ? `<div class="chip-row" style="margin-top:0.5rem;">${skills.map((s) => `<span class="chip">${s}</span>`).join('')}</div>` : '<p class="card-sub">No skills on file yet.</p>'}
    `;
  } catch (err) {
    // 404 = no resume registered yet, a normal state (see GET /candidates/me).
    card.innerHTML = `
      <div class="card-header">
        <h3 class="card-title" style="font-size:1.05rem;">Resume Summary</h3>
      </div>
      <p class="card-sub">No resume on file yet.</p>
      <a href="apply.html" class="btn-outline">Upload your resume</a>
    `;
  }
}

document.getElementById('accountForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const status = document.getElementById('accountFormStatus');
  status.textContent = '';
  status.className = 'form-status';
  try {
    const updated = await updateMyProfile({
      full_name: document.getElementById('fullName').value || null,
      phone: document.getElementById('phone').value || null,
      location: document.getElementById('location').value || null,
      headline: document.getElementById('headline').value || null,
      bio: document.getElementById('bio').value || null,
    });
    currentProfile = updated;
    renderHeader(updated);
    status.textContent = 'Saved.';
    status.className = 'form-status success';
  } catch (err) {
    status.textContent = err.message || 'Could not save your profile.';
    status.className = 'form-status error';
  }
});

document.getElementById('passwordForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const status = document.getElementById('passwordFormStatus');
  status.textContent = '';
  status.className = 'form-status';
  const currentPassword = document.getElementById('currentPassword').value;
  const newPassword = document.getElementById('newPassword').value;
  try {
    await changeMyPassword(currentPassword, newPassword);
    status.textContent = 'Password changed.';
    status.className = 'form-status success';
    document.getElementById('passwordForm').reset();
  } catch (err) {
    status.textContent = err.message || 'Could not change your password.';
    status.className = 'form-status error';
  }
});

(async function init() {
  renderNavbar();
  try {
    currentProfile = await fetchMyProfile();
  } catch (err) {
    if (err.status === 401) {
      window.location.href = 'login.html';
      return;
    }
    throw err;
  }
  renderHeader(currentProfile);
  fillAccountForm(currentProfile);
  if (currentProfile.user_type === 'recruiter') {
    renderRecruiterCard(currentProfile);
  } else {
    await renderCandidateCard();
  }

  if (window.location.hash === '#api-keys') {
    document.getElementById('api-keys').scrollIntoView({ behavior: 'smooth' });
  }
})();
</script>
</body>
</html>
```

- [ ] **Step 2: Manual verification**

Run the backend locally, sign up/log in as a candidate, navigate to `profile.html`: confirm the header, account form (save + reload persists), read-only resume card (or the "no resume" empty state), and password change form all work. Repeat signed in as a recruiter: confirm the company form appears and saves, and no resume card is shown. Confirm `profile.html#api-keys` scrolls to the placeholder card, and it is disabled.

- [ ] **Step 3: Commit**

```bash
git add pages/profile.html
git commit -m "feat(profile): add the profile page"
```

---

## Task 9: Full regression pass

**Files:** none (verification only)

- [ ] **Step 1: Run the entire backend test suite**

Run: `pytest tests/ -v`
Expected: PASS, including every test added in Tasks 1-5 and every pre-existing test file untouched by this plan.

- [ ] **Step 2: Confirm the design doc's non-goals were honored**

Grep for anything this plan should NOT have touched:

Run: `git diff main --stat` (or `git diff <base-branch> --stat` if not `main`)
Expected: no changes to `providers/llm/`, no new `user_api_keys` table, no `cryptography` dependency added to `requirements.txt` — all BYOK-specific and correctly deferred to the `byok` branch.

- [ ] **Step 3: No commit needed for this task** — it is verification only. If Step 1 or Step 2 surfaces a problem, fix it in the task that introduced it and re-run this task's steps.
