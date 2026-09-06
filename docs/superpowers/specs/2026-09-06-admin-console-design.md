# Admin Console — Design Spec

Date: 2026-09-06
Branch: `admin`
Status: Approved for planning

## 1. Motivation

OpenHire today has two account types, `candidate` and `recruiter`, and no
operator-facing surface at all: no way to see how many accounts or jobs
exist, no way to act on a user or posting that violates policy, and no way
to reproduce a user's bug report without their password. This spec adds a
third account type, `admin`, and a console for it covering:

1. **User management** — list/search users, deactivate/reactivate an
   account, change a user's role (including promoting to admin).
2. **Content moderation** — hide job postings, candidate profiles, and
   applications that violate policy, reversibly.
3. **Platform metrics** — a read-only dashboard of simple counts (users by
   type, jobs, applications).
4. **Support impersonation** — an admin can generate a session as a given
   user (to reproduce their exact view), with every impersonation logged.

This is one spec covering all four areas, but implementation (the plan
this spec feeds) is expected to land as several sequential tasks — user
management and auth gating first, since moderation, metrics, and
impersonation all depend on `require_admin` existing.

## 2. Non-goals

- No permission granularity within "admin" (no separate moderator/
  super-admin tiers). Any admin can do everything an admin can do.
- No admin self-service signup. There is no "admin" option on the public
  signup form, ever — see §4.
- No UI for browsing the audit log in phase 1 (the log is written and
  queryable via the repository/DB directly; a viewer page can be a later
  addition).
- No changes to candidate/recruiter-facing behavior other than the new
  `is_hidden` moderation flags being respected by existing listing
  endpoints (a hidden job/profile/application simply stops appearing in
  normal, non-admin listings — see §5.3).

## 3. Data model

### 3.1 `user_type` gains a third value

`UserRecord.user_type` (`repositories/interfaces.py`) becomes
`'candidate' | 'recruiter' | 'admin'`. No new fields needed on
`UserRecord` for the admin role itself — an admin has no admin-specific
profile data. The existing `is_active` field is reused for account
deactivation (already present, already respected by login: an inactive
user cannot authenticate — confirm/wire this in `AuthService` if not
already enforced).

### 3.2 Moderation flags

Add a nullable-safe boolean moderation flag to the two record types that
don't already have one:

- `JobRecord` already has `is_active: bool = True` — reused as-is; an
  admin-hidden job sets `is_active = False` via the same field a
  recruiter's own archive action uses. (A hidden-by-admin job is
  indistinguishable from a recruiter-archived one in phase 1; both mean
  "don't show this in active listings." This is an accepted simplification
  — see Open Questions if the distinction later matters.)
- `CandidateRecord` gains `is_hidden: bool = False`.
- `Application` (schemas/application.py) gains `is_hidden: bool = False`.

All three moderation actions are reversible (an admin can un-hide).

### 3.3 Audit log

New repository-layer record for impersonation accountability:

```python
class AuditLogRecord(BaseModel):
    log_id: str
    admin_id: str
    action: str          # e.g. "impersonate"
    target_user_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class AuditLogRepository(ABC):
    async def save(self, record: AuditLogRecord) -> AuditLogRecord: ...
    async def list_for_admin(self, admin_id: str) -> list[AuditLogRecord]: ...
```

`action` is a free-text field rather than an enum since impersonation is
the only action logged in phase 1, but the shape allows future admin
actions (e.g. "deactivate_user") to log through the same table without a
migration.

## 4. Admin account creation

There is no self-service path to becoming an admin. The public signup
form and `POST /auth/signup` continue to accept only `candidate` /
`recruiter` — `user_type == 'admin'` is rejected there unconditionally.

The **first** admin is created out-of-band: a small operator script
(`scripts/promote_admin.py`, run manually against the target database)
that takes an existing user's email and sets `user_type = 'admin'`. This
is a deploy-time operator action, not an application feature.

Once at least one admin exists, admins can promote **any** existing user
(candidate, recruiter, or another admin-to-be) to any role — including
`admin` — through the console's user-management screen
(`PATCH /admin/users/{id}`). There is no limit on the number of admins and
no separate "super-admin" tier that alone can create admins — see §2.

## 5. API

New router, `api/routes/admin.py`, mounted under `/admin`. Every route is
gated by a new `require_admin` dependency in `core/security.py`, modeled
directly on the existing `require_authenticated` (same JWT validation,
plus a check that `principal`'s resolved `UserRecord.user_type == 'admin'`
— returns 403 otherwise, not 404, since the route existing is not a
secret).

### 5.1 User management

- `GET /admin/users?query=&user_type=&is_active=` — paginated/simple list,
  filterable by role and active state. Returns `UserProfileResponse`-shaped
  rows (reuses the schema already added for the profile page — no
  password hash exposed).
- `PATCH /admin/users/{user_id}` — body may include `is_active` and/or
  `user_type`. Same "filter to allowed fields" pattern as
  `AuthService.update_profile`, just with a wider allowed set (admins may
  change fields a self-service profile update cannot). Rejects setting
  `user_type` to anything outside `{'candidate','recruiter','admin'}`
  with 400.

### 5.2 Moderation

- `GET /admin/jobs?include_hidden=true` — all jobs regardless of
  `is_active`, for the moderation view (the existing `JobRepository.
  list_jobs` already supports `include_archived`).
- `PATCH /admin/jobs/{job_id}` — `{"is_active": false}` to hide,
  `{"is_active": true}` to restore. Delegates to `JobRepository.archive`
  for hide; a new `JobRepository.restore` (or a generalized `set_active`)
  for un-hide.
- `GET /admin/candidates?include_hidden=true`,
  `PATCH /admin/candidates/{candidate_id}` — `{"is_hidden": bool}`.
- `GET /admin/applications?include_hidden=true`,
  `PATCH /admin/applications/{application_id}` — `{"is_hidden": bool}`.

Existing candidate/recruiter-facing listing endpoints (job search,
candidate-facing job list, recruiter's applications-for-job view, etc.)
are updated to exclude `is_hidden`/inactive records by default, matching
how archived jobs already behave.

### 5.3 Metrics

- `GET /admin/metrics` — returns simple counts:
  `{"users": {"candidate": n, "recruiter": n, "admin": n}, "jobs": {"active": n, "hidden": n}, "applications": {"total": n, "hidden": n}}`.
  Computed by calling `list_*`/count-shaped methods already on the
  existing repositories — no new aggregation infrastructure, no new
  tables. If a repository lacks an efficient count method (e.g. the
  in-memory/Postgres repos currently only expose `list_*`), add a `count`
  method to that interface rather than fetching full lists just to `len()`
  them, if the list is expected to grow large; for phase 1's simple counts
  either is acceptable and left to the implementer's judgment per
  repository.

### 5.4 Impersonation

- `POST /admin/impersonate/{user_id}` — validates the target user exists
  and is active, issues a normal access/refresh token pair for that user
  via the existing `AuthService`/JWT issuance path (so the resulting
  session is indistinguishable from the user's own login to the rest of
  the app), and writes an `AuditLogRecord` (`admin_id` = the calling
  admin, `action` = `"impersonate"`, `target_user_id` = the target).
  Returns the token pair. The frontend swaps the stored access/refresh
  tokens to the returned ones, so subsequent requests act as the target
  user — reversed by the admin logging back in as themselves.

## 6. Frontend

Single new page, `pages/admin.html`, following the existing
`profile.html`/`candidate.html` structure and shared `style.css` tokens.
Tabbed sections: **Users**, **Moderation**, **Metrics**. Linked from the
navbar dropdown (`renderNavbar` in `pages/js/app.js`) only when
`user_type === 'admin'` — candidates and recruiters never see the link.

- **Users tab**: searchable/filterable table, inline actions
  (deactivate/reactivate, role dropdown, "impersonate" button).
- **Moderation tab**: three sub-lists (jobs / candidates / applications)
  each with a hide/restore toggle.
- **Metrics tab**: the counts from `GET /admin/metrics` as simple stat
  tiles (reusing `dataviz`-style stat tile patterns from the existing
  design system if any exist, otherwise plain labeled numbers — no charts
  needed for simple counts).

Clicking "Impersonate" on a user row calls
`POST /admin/impersonate/{id}`, stores the returned tokens (same
mechanism `auth.js` uses after login), and redirects to that user's
landing page (candidate/recruiter dashboard per their `user_type`). A
persistent banner ("Viewing as {email} — [Return to admin]") is shown
while impersonating; "Return to admin" is a plain re-login as the
original admin (no token-swap-back needed since the admin's own tokens
were simply replaced, not retained — the admin re-authenticates
normally). This is the simplest correct behavior for phase 1; a
token-stacking "return to admin without re-login" flow is deferred (see
Open Questions).

## 7. Testing

Standard backend test coverage matching the profile-page precedent:
`require_admin` rejects non-admin/unauthenticated callers (401/403);
each endpoint's success path; role-change validation rejects invalid
`user_type` values; impersonation writes exactly one audit log row and
returns valid tokens for the target user; moderation hide/restore
round-trips and is reflected in existing public listing endpoints.

## 8. Open questions (non-blocking, flagged for the plan/implementer)

- Whether admin-hidden jobs should be distinguishable from
  recruiter-archived jobs (separate flag) is deferred — phase 1 treats
  them identically via the existing `is_active` field.
- "Return to admin" after impersonating re-authenticates rather than
  restoring the admin's original session token pair. If this proves
  annoying in practice, a later iteration can have the frontend retain
  the admin's original tokens in memory during an impersonation session
  instead of discarding them.
