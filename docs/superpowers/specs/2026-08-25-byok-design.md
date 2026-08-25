# Bring Your Own Key (BYOK) — Design

## Problem

OpenHire's pipeline currently only runs against a single LLM provider
(OpenAI) configured server-side via the `OPENAI_API_KEY` environment
variable (`config/settings.py`, `providers/llm/__init__.py`). There is no
way for a user to run the pipeline with their own API key, and no way to
use a provider other than OpenAI (or the mock provider).

We want anyone to be able to paste their own API key into OpenHire and
have the pipeline run using that key, for a wide range of LLM providers
(OpenAI, Groq, Gemini, NVIDIA NIM, Anthropic, and others), without
OpenHire ever persisting the key.

## Goals

- A user can paste an API key + pick a provider + model, and run the full
  hiring evaluation pipeline against that provider/key.
- Support for many providers "for free" rather than hand-writing a client
  per vendor.
- Keys are request-scoped only: used for one pipeline run, then discarded.
  Never written to disk, a database, logs, or browser storage.
- No regression to the existing CLI (`main.py`) / env-var-based flow.

## Non-goals

- Persisting keys per user account (no auth/accounts system in this PR).
- Multi-provider support for embeddings or audio processing (LLM only for
  now — those factories are untouched).
- Hosting/deploying the new API service (deploy config is out of scope;
  local `uvicorn` run is sufficient for this PR).

## Architecture

```
pages/index.html (form: provider, model, api key, job/resume input)
        |  fetch POST /api/run (CORS-enabled, different origin)
        v
api/main.py (FastAPI)
        |  builds LiteLLMProvider(provider, api_key, model)
        |  builds a fresh pipeline graph with that provider injected
        v
orchestration/graph.py: get_pipeline(llm_provider=...) -> compiled graph
        |
        v
agents/* (BaseAgent) -> LiteLLMProvider -> litellm.acompletion(...)
```

The key lives only in the request handler's local scope for the duration
of one `/api/run` call. It is never logged, never included in `AuditLog`
entries (those only capture agent inputs/outputs, not provider
construction args), and never written to disk.

## Provider layer

### `providers/llm/litellm_provider.py` (new)

`LiteLLMProvider(LLMProvider)`:

- `__init__(self, provider: str, api_key: str, model: str)` — stores a
  litellm-formatted model string (`f"{provider}/{model}"`) and the key.
- `generate(...)` / `generate_structured(...)` — same shape as
  `OpenAIProvider`, calling `litellm.acompletion(model=..., api_key=...,
  messages=..., response_format=...)`.
- `_classify(e)` — maps `litellm.exceptions.*` to `LLMTransientError`
  (rate limit, timeout, connection, 5xx) or `LLMPermanentError`
  (auth, bad request, not-found, everything else), mirroring
  `OpenAIProvider._classify`.

### `providers/llm/__init__.py`

`get_llm_provider(provider: Optional[str] = None, api_key: Optional[str] = None, model: Optional[str] = None) -> LLMProvider`:

- If `provider` or `api_key` is passed explicitly (the BYOK path), build
  and return a `LiteLLMProvider`.
- Otherwise, preserve today's behavior exactly: read `LLM_PROVIDER` from
  env, return `OpenAIProvider` (env key) or `MockLLMProvider`.

`OpenAIProvider` and `MockLLMProvider` are unchanged.

### `requirements.txt`

Add `litellm`, `fastapi`, `uvicorn`.

## Orchestration wiring

### `orchestration/graph.py`

- `create_pipeline_graph(llm_provider: Optional[LLMProvider] = None)` —
  passes `llm_provider` into every agent constructor (`BaseAgent` already
  accepts an optional `llm_provider` and falls back to
  `get_llm_provider()` when `None`).
- `get_pipeline(llm_provider: Optional[LLMProvider] = None)` — when
  `llm_provider` is given, always builds a fresh graph (bypasses the
  module-level singleton, since a singleton can't serve concurrent
  requests using different users' keys). When omitted, keeps today's
  cached-singleton behavior for the CLI path.

`main.py` keeps calling `get_pipeline()` with no arguments — unaffected.

## API

### `api/main.py` (new, FastAPI)

`POST /api/run`

Request body (Pydantic model):

```json
{
  "provider": "groq",
  "api_key": "...",
  "model": "llama-3.1-70b",
  "job": { ... same dict shape main.py.build_job_from_dict expects ... },
  "resumes": [ { ... } ],
  "interviews": { "candidate_id": { ... } }
}
```

- `api_key` is a required, non-empty string (Pydantic validation ->
  422 if missing).
- Handler builds a `LiteLLMProvider` from `provider`/`api_key`/`model`,
  calls `get_pipeline(llm_provider=...)`, runs it with the parsed
  job/resumes/interviews (reusing `main.py`'s existing
  `build_job_from_dict`/`build_resume_from_dict` helpers), and returns the
  resulting leaderboard + candidate reports as JSON.
- `LLMPermanentError` (bad key, unknown provider/model, etc.) is caught
  and returned as HTTP 401 with a message like `"authentication failed
  for provider groq"`, not a 500.
- Other unexpected errors -> HTTP 500 with a generic message (no key or
  internals leaked).
- CORS middleware enabled (configurable allowed origins, permissive
  default for local/dev use) since the landing page is statically hosted
  separately from this API per `deploy/`.
- No request/response logging of the body; uvicorn access logs configured
  to exclude bodies.

### `pages/index.html`

Add a form: provider `<select>`, model `<input>`, key `<input
type="password">`, and the existing job/resume inputs (or a minimal
subset), wired to `fetch('/api/run', ...)`. Key input is never written to
`localStorage`/cookies; it only lives in the in-page form state for the
submission.

## Testing

- `tests/test_litellm_provider.py` — `LiteLLMProvider` unit tests with
  `litellm.acompletion` mocked (no real network calls/keys in CI):
  success path, structured output, and exception classification
  (transient vs. permanent) for representative litellm exceptions.
- `tests/test_api.py` — FastAPI `TestClient` tests for `/api/run`: happy
  path (mocked provider/pipeline), missing `api_key` -> 422, provider
  auth failure -> 401.
- Orchestration test (new or extended) verifying `get_pipeline(llm_provider=...)`
  returns a fresh graph (not the cached singleton) and that the injected
  provider is the one agents actually use.

## Docs

- `docs/byok.md` (new): how to run the API locally
  (`uvicorn api.main:app --reload`), the request/response contract,
  supported provider strings (whatever litellm supports, with
  openai/groq/gemini/nvidia_nim/anthropic called out as examples), and an
  explicit statement that keys are never persisted.
- `README.md`: link to `docs/byok.md`, note the new optional API service
  alongside the existing CLI pipeline.
