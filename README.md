# OpenHire

An AI-powered interview platform for employer-side, high-volume candidate screening — built for the India campus hiring context (Tier 2/3 connectivity, multilingual candidates, high applicant volume).

OpenHire runs structured, voice-based interviews at scale, evaluates candidates against configurable rubrics using a specialized multi-agent pipeline, and gives recruiters an explainable, evidence-backed shortlist instead of a black-box score.

## Documentation

- [Multi-Agent System implementation](docs/multi-agent-system.md) — the Stage 3-level agent pipeline (orchestrator, evaluators, scoring, reporting) built ahead of schedule as a working foundation.

## Team

5-person team split across: **Frontend · Backend · Database · Multi-Agent System · Fullstack**

## Roadmap

Development is staged so every milestone is a working, demoable system — not a partial build. Stage 1 ships before Stage 2 begins, and so on.

### Stage 1 — Basic Prototype

A single, linear, single-candidate interview loop: a recruiter creates a job with a fixed question list, a candidate completes a voice interview via link, speech-to-text transcribes the answers, one LLM call scores the transcript against the job, and the recruiter sees a score and report.

Scope is deliberately narrow: one interview type, one language (English), no resume screening, no adaptive follow-up questions, no fallback ladder, one candidate at a time.

Stack: React web app, Node/Python API layer, cloud STT (e.g. Whisper API), a single LLM call for question delivery plus a single LLM call for scoring, and Postgres for jobs, candidates, interviews, transcripts, and scores. Deployed to a single environment.

**Goal:** prove the loop works end to end. Difficulty: Low–Medium.

### Stage 2 — Advanced

The scripted flow becomes adaptive: the AI asks conversational follow-up questions based on what the candidate actually said, evaluation moves to a rubric per competency instead of one number, recruiters can configure multiple interview types (technical / HR / aptitude) with role-configurable scoring weights, and both dashboards become functional — recruiters get a filterable, ranked shortlist, candidates get status tracking.

Adds conversation-state management, a JSON-configurable rubric builder, structured per-competency scoring output, a dashboard with filtering/sorting/drill-down, and recruiter/candidate auth and roles.

**Goal:** an adaptive, role-aware, multi-competency system with dashboards recruiters can actually use to make decisions. Difficulty: Medium–High.

### Stage 3 — Complete Multi-Agent System

The single do-everything agent splits into specialized agents: an **orchestrator** (runs the live conversation), a **technical/skill evaluator**, an **integrity agent** (conversational-probing analysis on the sealed transcript, not a detection classifier), a **scoring agent** (aggregates weighted rubric results), and a **report generation agent** (explainable report + leaderboard).

Evaluation agents run asynchronously on the sealed transcript after the interview ends — not live — keeping the candidate-facing interview fast and cost per interview manageable at volume. Adds an async task queue for fan-out (e.g. Celery/BullMQ), a vector DB for JD–resume context and rubric/question-bank lookup, defined agent I/O contracts, and per-agent audit logging.

**Goal:** a modular, auditable, specialized pipeline. Recruiters get a rich, evidence-backed report per candidate; integrity flags are surfaced for human review and never auto-reject. Difficulty: High–Very High.

### Stage 4 — Production

Everything needed to run this on a real Indian campus drive at volume: multilingual and code-mixed interviews, a connectivity fallback ladder (video → audio → async → phone call), bias testing across language/accent slices, explainability tied to transcript evidence, a mandatory human review gate before any hiring decision, India-only data residency with encryption and access control, monitoring/observability, and a scalable architecture that survives a burst of thousands of interviews in a single week — plus Naukri/ATS sync, WhatsApp/SMS delivery, and a per-interview INR credits billing model.

**Goal:** a hardened, compliant, India-ready product that can actually be sold and operated at scale. Difficulty: Very High.

## Difficulty at a glance

| Domain | Stage 1 | Stage 2 | Stage 3 | Stage 4 |
|---|---|---|---|---|
| Frontend | Medium | Medium | Medium | Medium–High |
| Backend | Medium | Medium–High | High | Very High |
| Database | Low | Medium | Medium–High | High |
| Multi-Agent System | Medium | High | Very High | Very High |
| Fullstack | Medium | Medium–High | High | High |

Multi-Agent System is the steepest curve on the team — plan pairing or backup coverage there from Stage 2 onward. Backend and Fullstack ramp hardest in Stage 4, when integrations, scale, and security all land at once.
