# Deployment plan

OpenHire is a student project right now, so the deployment stack is chosen for
cost (free tiers) over scale. Revisit this once there's real usage or budget.

## Current state

- **Landing page** (`pages/`) — static site on Render, see [`render.yaml`](render.yaml)
  and [`README.md`](README.md) for the connect steps.

## Planned stack (Stage 1 prototype)

| Layer | Provider | Why |
|---|---|---|
| Frontend (React app) | **Vercel** | Best free tier + DX for React; git-based deploys, no config |
| Backend (Node/Python API, STT + LLM orchestration) | **Render** | Persistent process — Vercel's serverless functions cap execution around 10-60s on the free/hobby plan, too short for STT/LLM calls in the interview loop |
| Database (Postgres) | **Supabase** or **Neon** | Render's free Postgres expires after 90 days; Supabase/Neon free tiers don't |

### Why not one provider for everything

Render alone (frontend + backend + DB) is possible but its Postgres free tier
expiring after 90 days is a real gotcha for a semester-long project. Railway
is a reasonable single-provider alternative if keeping everything in one
dashboard matters more than squeezing the best free tier out of each layer.

### Known tradeoffs

- **Cold starts:** Render's free web service spins down after 15 min idle;
  the next request eats a ~30-50s cold start. Fine for background use, bad
  for a live demo — either ping it on a schedule to keep it warm, or upgrade
  to the $7/mo starter plan before a demo/presentation.
- **Vercel function limits:** if any backend logic ends up on Vercel (e.g.
  a lightweight API route), keep it short-running — long STT/LLM calls belong
  on the Render service, not a Vercel function.

## Later stages

Stage 2+ (adaptive interviews, async task queue, vector DB — see the root
[`README.md`](../README.md)) will likely outgrow the free tiers. Revisit
provider choice once there's a usage pattern to size against, rather than
guessing now.
