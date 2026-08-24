# Role Documentation Design (Stage 1)

## Purpose

OpenHire's team is split into 5 roles: Frontend, Backend, Database, Multi-Agent
System, and Fullstack. This design specifies a set of role-charter documents
that let each team member know, in one file, what they own, what they depend
on from the other roles, and what they must provide back — scoped to Stage 1
of the roadmap (the basic prototype described in `README.md`).

These are working reference documents the team consults during the Stage 1
build, not a full technical spec of the system. They name interfaces
concretely enough to code against (endpoint shapes, table names, function
signatures) without going to full OpenAPI/DDL detail.

## Scope

Stage 1 only: a single, linear, single-candidate interview loop — recruiter
creates a job with a fixed question list, candidate completes a voice
interview via link, speech-to-text transcribes answers, one LLM call scores
the transcript, recruiter sees a score and report. No adaptive follow-ups, no
multi-agent split, no auth, one candidate at a time, one language (English).

Later stages (Advanced, Complete Multi-Agent System, Production) are out of
scope for this document set. When the team starts Stage 2, these files get a
new revision through their own design pass.

## File Structure

```
docs/roles/
  PROJECT_FLOW.md      shared Stage 1 end-to-end walkthrough
  frontend.md
  backend.md
  database.md
  multi-agent.md
  fullstack.md
```

Six files total. `PROJECT_FLOW.md` is the shared entry point; the five role
files are the source of truth for exact interface shapes.

## Per-Role File Template

Every role file (`frontend.md`, `backend.md`, `database.md`,
`multi-agent.md`, `fullstack.md`) follows the same section order, so the team
can scan any file and immediately find the same information in the same
place, and so cross-role dependencies are easy to verify (if role X depends
on something role Y doesn't list under "Provides," the gap is visible without
cross-referencing prose).

1. **Mission** — one paragraph: what this role is responsible for in Stage 1.
2. **Owns** — the concrete artifacts this role builds and is accountable for.
3. **Depends On** — what this role needs from each other role, named
   explicitly per dependency (e.g. "Backend: `POST /interviews/:id/answers`
   returning `{transcript_id}`").
4. **Provides** — the interfaces this role exposes to others, in the same
   concrete shape it's referenced by in other files' "Depends On" sections
   (endpoint signatures, table names + key columns, function/module
   signatures). Not full OpenAPI/DDL — enough to code against.
5. **Stage 1 Tech Stack** — the specific tools this role touches, drawn from
   the Stage 1 stack in `README.md`.
6. **Definition of Done** — a checklist of what "this role's Stage 1 slice
   works" means, tied to the README's Stage 1 goal (one candidate completes a
   full interview end-to-end and a recruiter sees a score and report).

Every "Depends On" entry in one file must have a matching "Provides" entry in
the named role's file, using the same interface shape. This consistency is
checked during the self-review pass and again before the files are
considered final.

## PROJECT_FLOW.md Content

A single numbered walkthrough of one Stage 1 interview end-to-end, each step
tagged with its owning role(s):

1. Recruiter creates a job with fixed questions — Frontend + Backend
2. Candidate opens interview link — Frontend
3. Candidate answers via voice; audio sent to backend — Frontend → Backend
4. Backend calls STT, stores transcript — Backend + Database
5. Backend calls the scoring LLM against transcript + job rubric — Backend,
   using the prompt/I-O contract owned by Multi-Agent
6. Score + report persisted — Database
7. Recruiter views score/report — Frontend ← Backend

Each step links to the "Provides" section of the role file that owns it,
rather than repeating interface detail — `PROJECT_FLOW.md` stays a short
narrative map, and the role files remain the single source of truth for exact
shapes.

## Role Responsibilities (Stage 1)

- **Frontend** — recruiter job-creation form, candidate interview page (voice
  capture UI), recruiter results view. React.
- **Backend** — REST API layer: job CRUD, interview session lifecycle, STT
  orchestration call, scoring LLM call, persistence writes. Node/Python.
- **Database** — Postgres schema owning `jobs`, `candidates`, `interviews`,
  `transcripts`, `scores`, and migrations.
- **Multi-Agent** — Stage 1 has no real multi-agent split yet (that's Stage
  3 per the README). This role owns the *prompt design and I/O contract* for
  the two Stage 1 LLM calls (question delivery, scoring), designed so it is a
  natural seed for the Stage 3 agent split rather than something that gets
  rewritten from scratch.
- **Fullstack** — glue and delivery: env/deploy config, wiring
  frontend↔backend integration, STT/LLM provider credentials and config, and
  unblocking whichever role is stuck. This role's Stage 1 job is explicitly
  "keep the end-to-end loop working," not one fixed slice of functionality.

## Definition of Done (document set)

The document set is done when:

- All 6 files exist at the specified paths.
- Every role file follows the template in full (no missing sections).
- Every "Depends On" entry has a matching "Provides" entry elsewhere, in a
  consistent interface shape.
- `PROJECT_FLOW.md`'s 7 steps each link to the correct role file section.
- Each role file's own Definition of Done checklist, taken together, adds up
  to the README's stated Stage 1 goal: one candidate completes a full
  interview and a recruiter sees a score and report.

## Out of Scope

- Full OpenAPI specs, DDL, or agent prompt text — the files name interface
  shapes, they don't fully specify implementations.
- Stage 2–4 content — these files are Stage 1 only and will be revised in a
  future design pass when Stage 2 begins.
- Auth, multilingual support, adaptive questioning, and anything else
  explicitly out of scope for Stage 1 per `README.md`.
