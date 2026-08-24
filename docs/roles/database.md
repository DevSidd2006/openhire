# Database — Stage 1

## Mission

Own the Postgres schema for Stage 1: jobs, candidates, interviews,
transcripts, and scores, plus their migrations. Stage 1 has no auth tables
and no rubric/competency breakdown — those arrive in Stage 2.

## Owns

- Postgres schema and migrations for `jobs`, `candidates`, `interviews`,
  `transcripts`, `scores`
- Referential integrity between these tables (foreign keys, not-null
  constraints on required fields)

## Depends On

Database is a leaf role in Stage 1 — no other role file should have a
"Depends On" entry naming Database, other than for its own tables listed
under Provides.

## Provides

- `jobs(id, title, questions jsonb, created_at)`
- `candidates(id, name, email, created_at)`
- `interviews(id, job_id, candidate_id, link_token, status, created_at)`
  — `job_id` FK → `jobs.id`, `candidate_id` FK → `candidates.id`
- `transcripts(id, interview_id, question_index, answer_text, audio_url, created_at)`
  — `interview_id` FK → `interviews.id`
- `scores(id, interview_id, overall_score, report_text, created_at)`
  — `interview_id` FK → `interviews.id`

## Stage 1 Tech Stack

Postgres. Migrations tracked in whatever migration tool the Backend role's
stack supports natively (e.g. `node-pg-migrate` or `alembic`) — coordinate
the choice with Backend rather than picking independently.

## Definition of Done

- [ ] All 5 tables exist with the columns and foreign keys listed above.
- [ ] A migration can be run from empty to current schema in one command.
- [ ] Backend's Stage 1 endpoints can read and write every table listed here
      without schema errors.
