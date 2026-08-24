# Nonilion integration — later phase

[Nonilion](https://www.nonilion.com/) is OpenHire's sponsor — a spatial
coworking platform where human teams and AI agents work side by side.
OpenHire's multi-agent architecture (see the root [`README.md`](../README.md))
is a natural fit for that positioning, which is part of why they're sponsoring
the project.

## Build order

1. **Ship OpenHire standalone first**, following its own staged roadmap
   (Stage 1 → Stage 4 in the root README). Deployment for this phase is
   covered by [`DEPLOYMENT.md`](DEPLOYMENT.md) — Vercel frontend, Render
   backend, Supabase/Neon DB.
2. **Integrate into Nonilion afterward, one component at a time** — not
   up front, and not by restructuring OpenHire's stack to pre-emptively
   match Nonilion's before there's a concrete spec to build against.

## What's known vs. not

- Nonilion's public site (`nonilion.com`) documents no API/SDK for external
  agent integration. The `Agents` and `Integrations` sections require a
  signed-in account and aren't visible from the outside.
- Nonilion's own stack, for reference: Next.js/React/Three.js frontend,
  Node.js/Postgres/Prisma backend, LiveKit for audio/video, integrations
  with Google Calendar/Stripe/Clerk.
- Concrete integration details — API access, an agent-registration flow,
  whatever the actual mechanism turns out to be — need to come from the
  sponsor relationship directly, not be guessed at from the marketing site.

## Next step

Once Nonilion shares real integration docs/API access, revisit this file
with the actual mechanism and update the build order above with concrete
steps.