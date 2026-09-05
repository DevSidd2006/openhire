# Bring Your Own Key (BYOK) — Design

Supersedes `docs/superpowers/specs/2026-08-25-byok-design.md`, which was
written when OpenAI was the only provider and which explicitly ruled out
both per-user key persistence and any dependence on an accounts system.
Both of those constraints have since been overtaken: there are four
native providers, and there is a real auth system. The August spec should
be treated as historical.

This design also builds on
`docs/superpowers/specs/2026-09-06-profile-page-design.md`, which adds
`pages/profile.html` with a placeholder "API Keys" card
(`id="api-keys"`) and states explicitly that "the `byok` branch replaces
this card's body and nothing else." This spec does not create a new
profile page or duplicate that page's account-fields work
(`GET/PATCH /auth/me`, password change, navbar dropdown) — it depends on
that page's shell existing and owns only the body of its API Keys card
plus the backend behind it. If the profile-page branch has not merged by
the time this is implemented, a minimal standalone card is an acceptable
stopgap, replaced once the shared page lands.

## Problem

Every LLM call OpenHire makes today runs on OpenHire's own provider
credentials, resolved from environment variables in `config/settings.py`
via `get_llm_provider()`. When that shared quota is exhausted, every user
is blocked at once and there is nothing a user can do about it.

We want a recruiter to be able to paste their own provider API key into a
profile page, pick their provider, and have the platform carry on working
exactly as it does now — but spending their quota instead of ours.

This runs on production. The dominant constraint is that shipping it must
not change behaviour for anyone who has not opted in.

## Goals

- A recruiter can save a provider + API key + optional model, and every
  LLM call made on their behalf then uses that key.
- Saved keys survive logout, so server-side work that outlives a browser
  session (evaluations dispatched after a candidate finishes) still runs
  on the right key.
- A user's key never leaves the server in plaintext and is never
  returned by any endpoint, logged, or written to an audit record.
- With the feature disabled, the system is byte-for-byte identical to
  today.

## Non-goals

- Multiple simultaneous credentials per user. One active credential per
  user; saving again replaces it.
- BYOK for embeddings, audio/voice, or vector store providers. LLM only.
- Adding new provider vendors. The dropdown offers exactly the four
  native providers that already exist and are already tested.
- Per-key usage metering or cost reporting.

## Decisions taken

Four decisions were settled before design and drive everything below.

1. **Keys are persisted, encrypted at rest, per user.** The alternative
   (session-only or browser-only) cannot serve background work, which is
   precisely when a user would be rate-limited.
2. **A saved user key always wins.** The system key is used only when the
   user has none. There is no "try ours first, then theirs" path: it
   would double latency on failure and make behaviour vary by time of
   day.
3. **Only the four existing native providers.** No `litellm`. Each of
   `openai`, `gemini`, `groq`, `nvidia_nim` already has a client with
   tested error classification and structured-output handling.
4. **A failing user key falls back to the system key and warns.** A
   candidate part-way through an interview must never be shown a
   recruiter's billing problem.

## Scope: who can use BYOK

Recruiters only. `users.user_type` is `candidate | recruiter`; the
endpoints below reject a candidate principal with 403. Recruiters own the
jobs and the spend, and a candidate has no reason to supply a key. This
also keeps the blast radius small: candidate-facing interview traffic
resolves a provider the same way it does today unless the recruiter who
owns the job has a credential.

The profile page's API Keys card is reached by both roles (it sits in
the same card slot regardless of `user_type`), so a candidate viewing
their profile sees the card with an explanation that BYOK is a
recruiter feature rather than a working form. The frontend distinguishes
on `user_type` from the already-loaded profile response; the backend
enforces the same rule independently via the 403 above, since the
frontend check is not a security boundary.

## Feature flag and failure-closed behaviour

The entire feature is gated on a single environment variable,
`BYOK_ENCRYPTION_KEY`, holding a urlsafe-base64 32-byte Fernet key.

- Unset: BYOK is off. The credential table is never read, no context
  variable is set, no wrapper is constructed, and the profile section is
  hidden. Behaviour is identical to today.
- Set: the feature is live.

Failing closed matters more than failing open here. If the variable is
missing or malformed at startup, the application logs one warning and
runs without BYOK rather than refusing to boot — an encryption
misconfiguration must not take production down.

## Storage

New table, added to `repositories/postgres/schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS user_llm_credentials (
    user_id       text PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    provider      text NOT NULL CHECK (provider IN ('openai','gemini','groq','nvidia_nim')),
    model         text,
    encrypted_key bytea NOT NULL,
    key_hint      text NOT NULL,
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active','failed')),
    last_error    text,
    last_error_at timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz
);
```

`user_id` as the primary key enforces "one credential per user" in the
schema rather than in application code. `ON DELETE CASCADE` means
deleting a user disposes of their key with no extra cleanup path.

`key_hint` is a display-only fragment (last four characters, prefixed with
an ellipsis) so the profile page can show *which* key is saved without
ever holding the key. It is derived once at save time.

### Encryption

`cryptography`'s `Fernet` (AES-128-CBC with an HMAC-SHA256 authentication
tag, with an in-token timestamp). Chosen over hand-rolled AES because it
is authenticated by construction and near-impossible to misuse.

The plaintext key exists in three places only: the HTTPS request body on
save, a local variable during encryption, and a local variable during
provider construction. It is never assigned to a field on a long-lived
object, never included in a Pydantic response model, and never passed to
a logger.

Rotation is documented but manual: re-encrypting all rows under a new
master key is an offline script, not an endpoint.

## Provider construction

`providers/llm/__init__.py` gains a pure constructor and keeps its
existing entry point:

```python
def build_provider(name: str, api_key: str, model: str | None = None) -> LLMProvider:
    """Construct a provider from explicit values. Reads no environment."""

def get_llm_provider() -> LLMProvider:
    """Unchanged signature. Resolves per-request BYOK first, else env."""
```

`build_provider` returns the same four classes already in use, so BYOK
inherits their tested error classification and structured-output
behaviour rather than introducing a parallel client. Model defaults per
provider come from settings when the user leaves the model field blank.

## Reaching the agents

This is the only structurally interesting part of the change.

`agents/base.py:42` resolves its provider as
`llm_provider or get_llm_provider()`, and that call happens deep inside a
tree of agents constructed by `core/container.py`. Threading a
`user_id`, or a provider, through every agent constructor and every
container factory would be a wide diff across production code paths whose
only purpose is to carry one optional value.

Instead, a `ContextVar` in a new `core/llm_context.py`:

- A FastAPI dependency on authenticated recruiter routes resolves the
  caller's credential once and sets the context variable for the duration
  of the request.
- `get_llm_provider()` reads the context variable first and falls back to
  the environment when it is unset.

This touches one line at the resolution seam and no agent signatures.
Context variables are copied into tasks created with
`asyncio.create_task`, so the fire-and-forget work in
`services/evaluation_dispatcher.py` inherits the caller's provider
without further plumbing.

### The boundary, stated plainly

Ambient context only reaches code that runs inside the request or inside
a task spawned from it. Two cases fall outside it and must resolve
explicitly:

- `main.py` (the CLI) and any future out-of-process queue worker see no
  context variable and correctly use the system key.
- Long-lived interview sessions outlive the request that created them.
  These store the owning recruiter's `user_id` on the session and
  re-resolve the provider at use time, rather than depending on ambient
  context that will have been torn down.

Relying on the context variable for those cases would be a latent bug, so
the design does not.

## Failure handling

`providers/llm/fallback.py` adds a thin decorator:

```python
FallbackLLMProvider(primary, secondary, on_primary_failure)
```

It delegates to `primary`. On `LLMPermanentError` (revoked or malformed
key) or on a rate-limit `LLMTransientError`, it completes the call on
`secondary` and invokes the callback, which marks the credential
`status='failed'` with the reason and timestamp. Other transient errors
propagate so `BaseAgent`'s existing retry logic handles them as it does
today; the wrapper must not swallow the retry signal.

The profile page surfaces `status='failed'` with `last_error` so the user
learns their key stopped working. The interview or evaluation that
triggered the fallback still completes.

## API

All three require an authenticated recruiter principal (`403` for a
candidate). None of them ever returns the key.

- `GET /api/me/llm-credential` → `{provider, model, key_hint, status,
  last_error, last_error_at}`, or `204` when none is saved.
- `PUT /api/me/llm-credential` → body `{provider, api_key, model?}`.
  Validates, encrypts, upserts. Returns the same shape as `GET`.
- `DELETE /api/me/llm-credential` → removes the row; the user reverts to
  the system key.

### Validation on save

`PUT` makes one cheap live call (a few-token completion) with the
submitted key before persisting. An invalid key is rejected at the
profile page with a clear message rather than being discovered later by a
candidate mid-interview. A validation failure returns `400` with the
provider's reason, and nothing is written.

Request bodies for these routes are excluded from any request logging.

## Profile page integration

This spec fills in the body of the API Keys card
(`id="api-keys"` in `pages/profile.html`) defined by the profile-page
spec. It does not touch that page's shell, navbar dropdown, identity
header, account-details card, role card, or password card.

Card body, matching the page's existing design vocabulary
(`.card`, `.form-group`, `.form-control`, `.btn-primary`, `.tag`):

- **Recruiter, no credential saved**: provider `<select>` (OpenAI,
  Gemini, Groq, NVIDIA NIM), model `<input>` (optional, placeholder
  showing the provider default), API key `<input type="password"
  autocomplete="off">`, Save button.
- **Recruiter, credential saved**: provider, model, and `key_hint` shown
  read-only, a Remove button, and — when `status='failed'` — a warning
  banner quoting `last_error`.
- **Candidate**: the card renders with its controls disabled and a line
  explaining BYOK is available for recruiter accounts. No request to
  the BYOK endpoints is made for a candidate principal.

Save calls `PUT /api/me/llm-credential` and reports success or failure
inline, matching how the account-details card reports its own save
result — no page reload, no global banner. The key is submitted once
over HTTPS and never written to `localStorage`, `sessionStorage`, or a
cookie.

## Testing

- Encryption round-trip; ciphertext differs across saves of the same key;
  decryption under a wrong master key fails loudly.
- No-leak tests: the plaintext key appears in no endpoint response, no
  log record, and no `AuditLog` entry.
- `build_provider` returns the right class per name and reads no
  environment.
- Fallback: permanent error routes to secondary and flags the credential;
  an ordinary transient error still propagates to `BaseAgent`'s retry.
- Context: set/unset, and inheritance into an `asyncio.create_task` child.
- Endpoints: happy path, candidate gets 403, invalid key gets 400 and
  writes nothing, `GET` never echoes the key.
- **Flag-off regression**: with `BYOK_ENCRYPTION_KEY` unset, the existing
  suite passes unchanged and `get_llm_provider()` resolves exactly as it
  does today.

## Deployment

1. Generate a Fernet key and set `BYOK_ENCRYPTION_KEY` on Render. The
   value is generated and pasted by a human; it is never committed,
   echoed into a shell history in this repo, or handled by tooling.
2. Apply the `user_llm_credentials` table.
3. Deploy. Until step 1 is done the feature is simply inert.

Losing the master key makes every stored credential undecryptable. That
is recoverable — users re-enter their keys — but the rows must then be
cleared, so the rotation script also has a "drop all credentials" mode.

## Risks

- **A stored key is a new secret we hold.** Mitigated by encryption at
  rest, never returning it, and cascade deletion — but the honest
  statement is that this changes OpenHire's security posture, which the
  August spec avoided on purpose. Accepted deliberately, because
  session-scoped keys cannot serve background evaluation work.
- **Ambient context is easy to misuse.** A future call site that resolves
  a provider outside a request will silently get the system key. The two
  known cases are handled explicitly above; new background paths must do
  the same.
- **Fallback masks a broken key.** A user could sit on `status='failed'`
  and quietly consume OpenHire's quota. The banner is the mitigation; a
  future improvement is to stop falling back after N consecutive
  failures.
