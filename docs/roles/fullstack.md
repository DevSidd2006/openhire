# Fullstack — Stage 1

## Mission

Keep the Stage 1 end-to-end loop actually working: environment and deploy
configuration, wiring the frontend↔backend integration, provider credentials
for STT and the scoring LLM, and unblocking whichever role is stuck. Unlike
the other four roles, this role's Stage 1 job is not one fixed slice of
functionality — it's "the loop works end-to-end," which means picking up
whatever's missing between the other four roles' pieces.

## Owns

- Deployment configuration for the single Stage 1 environment
- The shared `.env` schema (below) and how each role's local dev setup uses it
- Frontend↔Backend integration wiring (CORS, base API URL config, etc.)
- STT and LLM provider account setup and credential distribution

## Depends On

- **Frontend:** a working build that can be pointed at a configurable API
  base URL.
- **Backend:** a working service that reads its config (DB connection, STT
  key, LLM key) from environment variables rather than hardcoded values.
- **Database:** a migration command that can be run against a fresh Postgres
  instance during deploy.
- **Multi-Agent:** the LLM provider and model choice for `buildDeliveryPrompt`
  and `scoreTranscript`, so the right API key and quota can be provisioned.

## Provides

- Shared `.env` schema, used by Backend and referenced by Frontend's build
  config:
  - `DATABASE_URL` — Postgres connection string (Database)
  - `STT_API_KEY` — cloud STT provider key (Backend)
  - `LLM_API_KEY` — scoring/delivery LLM provider key (Backend, Multi-Agent)
  - `API_BASE_URL` — the deployed backend's base URL (Frontend)
- One deployed Stage 1 environment that all 5 roles can point their local
  work at for integration testing.

## Stage 1 Tech Stack

Whatever single-environment deploy target the team picks (e.g. a single VM
or a platform-as-a-service target) — one environment only for Stage 1, no
staging/prod split yet.

## Definition of Done

- [ ] A fresh clone of the repo, given the `.env` values above, can run the
      full Stage 1 loop locally: create a job, complete an interview as a
      candidate, and see a score as a recruiter.
- [ ] The same loop works against the deployed Stage 1 environment, not just
      locally.
- [ ] Every other role's Definition of Done in this document set is
      satisfied end-to-end, not just in isolation.
