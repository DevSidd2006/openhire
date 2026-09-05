# Profile Page — Design

## Problem

OpenHire has authentication (`api/routes/auth.py`, `services/auth_service.py`)
but no profile. A `users` row holds only `user_id`, `email`, `password_hash`,
`user_type`, and `is_active`. There is nowhere for a person to see or edit who
they are, and no way to change a password after signup.

The gap is already visible in the UI: `renderNavbar` (`pages/js/app.js:231`)
renders `user.name`, but signup never collects a name, so the navbar shows
whatever the frontend happened to cache.

Recruiters have no place to record a company. Candidates have rich profile
data — it lives on their parsed resume — but no page ever shows it back to
them.

## Goals

- A signed-in user, candidate or recruiter, can view and edit their own
  profile from a dedicated page reached from the top right of every page.
- The page shows all information related to that user, with role-appropriate
  sections.
- A user can change their password.
- The page reserves a clearly positioned slot for the Bring Your Own Key
  section, so the BYOK branch can fill it without restructuring the page.

## Non-goals

- **BYOK key storage, encryption, validation, and management.** All
  key-related work belongs to the `byok` branch. This branch ships only an
  inert placeholder card.
- Viewing another user's profile. Every endpoint here is self-scoped to the
  authenticated principal.
- Avatar image upload. `avatar_url` is stored, but the page renders initials;
  hosting/uploading images is a separate concern.
- Editing candidate resume data. That is the resume's job (see below).

## Key constraint: the resume is the candidate profile

`CandidateRecord` (`repositories/interfaces.py:378`) documents this
explicitly: the resume IS the candidate profile everywhere else in the
codebase — agents, the interview engine, and the matching agent all key off
`ParsedResume.candidate_id`. `ParsedResume` (`schemas/resume.py:51`) already
carries name, email, phone, location, summary, education, work experience,
projects, certifications, skills, technologies, languages, and total
experience years.

So this design does NOT copy skills or experience onto `users`. Doing so
would introduce the second, competing candidate representation that
`CandidateRecord` was written to avoid, and the two copies would drift the
moment a candidate uploaded a new resume.

Instead:

- `users` holds **account-level** fields, owned by the account.
- Candidate resume data is **read** from `GET /candidates/me` and rendered
  **read-only**, with a link to `apply.html` to update it by uploading a new
  resume.
- Recruiter statistics are **derived** at render time from `GET /jobs`, not
  stored as counters that can go stale.

## Data model

### `users` — new columns

All nullable, so existing rows migrate without a backfill.

| Column | Type | Applies to |
|---|---|---|
| `full_name` | text | both |
| `phone` | text | both |
| `location` | text | both |
| `headline` | text | both |
| `bio` | text | both |
| `avatar_url` | text | both |
| `company_name` | text | recruiter |
| `company_website` | text | recruiter |
| `company_role` | text | recruiter |

Role-specific columns live on the same table rather than in a side table: the
set is small, always fetched together with the user, and never queried
independently. A recruiter's three columns stay null for a candidate.

`UserRecord` (`repositories/interfaces.py:440`) gains the same fields as
`Optional[str] = None`. Both implementations are updated:
`repositories/memory.py` and `repositories/postgres/repository.py`.

Nothing about `user_id`, `email`, `password_hash`, `user_type`, `is_active`,
`created_at`, or `updated_at` changes.

## API

The endpoints are user-account operations, so they join the existing
`auth` router (`api/routes/auth.py`, prefix `/auth`) rather than opening a
new router. They are gated by `require_authenticated`
(`core/security.py:227`), the dependency already used by
`GET /candidates/me`, and resolve the subject from
`principal.subject_id` — never from a client-supplied id.

### `GET /auth/me` -> `UserProfileResponse`

Returns the authenticated user's full profile: every column above plus
`user_id`, `email`, `user_type`, and `created_at`. Never returns
`password_hash`.

This exists for the same reason `GET /candidates/me` does: the frontend
otherwise knows only what the login response handed it once and cached, which
is how the candidate dashboard previously lost its `candidate_id`.

### `PATCH /auth/me` -> `UserProfileResponse`

Partial update. Request model `UpdateProfileRequest` has every profile field
optional; omitted fields are left unchanged, and an explicit `null` clears a
field.

Rules:

- Only the profile columns listed above are patchable. `email`, `user_type`,
  `is_active`, and `password_hash` are absent from the request model, so a
  client cannot reach them.
- Recruiter-only fields (`company_name`, `company_website`, `company_role`)
  sent by a candidate are rejected with 400 `validation_error`, and the
  request is rejected as a whole rather than silently partially applied.
- `updated_at` is set on every successful patch.

### `POST /auth/me/password` -> 204

Body: `current_password`, `new_password` (min length 8, matching
`SignupRequest`). Verifies the current password through `AuthService`'s
existing hashing path and rejects a mismatch with 401 `unauthorized`. Neither
password appears in any log line.

Request/response models go in `schemas/auth.py` alongside the existing
auth schemas.

## Page

### `pages/profile.html`

Standard page shell: `navbarContainer` div, `main.container`, `css/style.css`,
`js/app.js`. Built from the existing design vocabulary — `.card`,
`.card-header`, `.form-group`, `.form-control`, `.btn-primary`,
`.tag` — so the only new CSS is an initials avatar circle and the navbar
dropdown.

Cards, top to bottom:

1. **Identity header** — initials avatar, full name (falling back to the email
   local part), email, role tag, and "Member since <created_at>".
2. **Account details** — editable: full name, phone, location, headline, bio.
   Save issues `PATCH /auth/me` and reports success or failure inline, with no
   page reload.
3. **Role card**
   - *Recruiter*: editable company name, company website, company role;
     plus derived "N jobs posted, M active" from `GET /jobs`.
   - *Candidate*: read-only resume summary from `GET /candidates/me` —
     skills as chips, total experience years, most recent role, education —
     and a recent-applications list. When no resume is on file (a normal
     state between signup and first upload, per `GET /candidates/me`'s 404
     contract), an empty state links to `apply.html`.
4. **Security** — change password form calling `POST /auth/me/password`.
5. **API Keys** (`id="api-keys"`) — placeholder on this branch: heading, a
   one-line explanation of bring-your-own-key, and a disabled control marked
   "coming soon". The `byok` branch replaces this card's body and nothing
   else.

### Navbar entry point

In `renderNavbar` (`pages/js/app.js:231`), the `.nav-right` user badge becomes
a click-toggled dropdown:

- **Profile** -> `profile.html`
- **API Keys** -> `profile.html#api-keys`
- divider
- **Sign out** -> the existing `logoutUser()`

The menu closes on outside click and on Escape. The standalone `Exit` button
is absorbed into it. Signed-out users keep the existing "Sign In" button.

## Error handling

Server-side errors use the project's existing error types
(`NotFoundError`, `UnauthorizedError`, validation errors) so they render
through the established handler shape. The page surfaces failures inline per
card rather than as a global banner, so a failed password change does not
obscure a successful profile save. A 401 from any call sends the user to
`login.html`, matching the behavior elsewhere in `app.js`.

## Testing

Following the structure of `tests/test_backend_*.py`:

- `GET /auth/me` returns the full profile for a candidate and for a recruiter.
- `GET /auth/me` never includes `password_hash`.
- `PATCH /auth/me` updates only the supplied fields and leaves the rest.
- `PATCH /auth/me` cannot change `email`, `user_type`, or `is_active`.
- `PATCH /auth/me` rejects recruiter-only fields from a candidate with 400.
- `POST /auth/me/password` succeeds with the correct current password and
  rejects a wrong one with 401.
- All three endpoints reject an unauthenticated caller.
- The memory `UserRepository` round-trips every new field.
