-- OpenHire PostgreSQL schema (V1).
--
-- Backs the six repository interfaces in repositories/interfaces.py:
-- JobRepository, CandidateRepository, ApplicationRepository,
-- SessionRepository, TranscriptRepository, EvaluationRepository.
-- Implemented by repositories/postgres/repository.py.
--
-- This is the schema approved after the read-only persistence audit and
-- the schema-design proposal that followed it, with three V1-specific
-- decisions:
--   - no generated `weighted_final_score` column on `evaluations` yet
--     (the leaderboard query computes/sorts on it in application code for
--     now; add the generated column in a later revision if that becomes a
--     bottleneck)
--   - no denormalized `jobs.title` / `candidates.candidate_name` columns -
--     no current repository method filters or sorts on either, so they are
--     not added "because it's possible"
--   - `docs/roles/database.md`'s older 5-table plan (no `applications`
--     table, no `evaluations` table) is superseded by this schema, which
--     matches the CURRENT backend's actual repository contracts instead.
--
-- Every table/column here traces to a concrete field on an existing
-- Pydantic model or a concrete repository method - nothing is invented.
-- See the schema proposal for the full column-by-column justification.
--
-- Idempotent: safe to run against an already-migrated database (every
-- statement is IF NOT EXISTS / ON CONFLICT-safe), so this file doubles as
-- the "run from empty to current schema in one command" migration the
-- database role's Definition of Done asks for.

BEGIN;

-- ---------------------------------------------------------------------
-- jobs — JobRecord (repositories/interfaces.py) wrapping JobDescription
-- (schemas/job.py). `job` holds the whole JobDescription (title,
-- description, department, level, required/preferred skills and
-- qualifications, experience_years, responsibilities, competencies[],
-- interview_topics[], evaluation_rubric, posting_date, closing_date) as
-- JSONB - no repository method queries into an individual field of it.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    job_id      text PRIMARY KEY,
    job         jsonb NOT NULL,
    is_active   boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz
);

-- Backs JobRepository.list_jobs(include_archived): "newest first",
-- filtered to is_active unless archived postings were asked for.
CREATE INDEX IF NOT EXISTS idx_jobs_is_active_created_at
    ON jobs (is_active, created_at DESC);

-- ---------------------------------------------------------------------
-- users — User authentication and profile information for candidates
-- and recruiters. Core table for the authentication system.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    user_id text PRIMARY KEY,
    email text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    user_type text NOT NULL CHECK (user_type IN ('candidate', 'recruiter')),
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz
);

-- Backs user lookups by email (login).
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- ---------------------------------------------------------------------
-- refresh_tokens — Refresh token storage for JWT-based authentication.
-- Tracks token validity and expiration for session management.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES users(user_id),
    token_hash text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Backs token lookups by user (session invalidation, token rotation).
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user_id ON refresh_tokens(user_id);

-- ---------------------------------------------------------------------
-- candidates — CandidateRecord wrapping ParsedResume (schemas/resume.py).
-- Same JSONB reasoning as `jobs.job`: education/work_experience/projects/
-- certifications are never queried by an individual entry anywhere in the
-- backend.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id    text PRIMARY KEY,
    resume          jsonb NOT NULL,
    used_fallback   boolean NOT NULL DEFAULT false,
    parse_warning   text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz
);

-- Backs CandidateRepository.list_candidates(): "newest first".
CREATE INDEX IF NOT EXISTS idx_candidates_created_at
    ON candidates (created_at DESC);

-- ---------------------------------------------------------------------
-- applications — Application (schemas/application.py), stored directly
-- with no wrapper record, matching ApplicationRepository's contract.
--
-- `session_id`'s foreign key is added further down, AFTER `sessions`
-- exists: applications.session_id and sessions.application_id are the
-- current backend's own redundant bidirectional link (both set once at
-- creation, never reconciled against each other - see
-- SessionRecord's docstring in repositories/interfaces.py), so both
-- columns are kept, unmodified, as two independent nullable foreign keys
-- rather than collapsed to one direction - collapsing them would require
-- changing SessionRecord/ApplicationRepository's contract, which this
-- schema is explicitly not allowed to do.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS applications (
    application_id  text PRIMARY KEY,
    job_id          text NOT NULL REFERENCES jobs (job_id),
    candidate_id    text NOT NULL REFERENCES candidates (candidate_id),
    -- Exact ApplicationStatus values (schemas/application.py) - lowercase,
    -- matching the Enum's actual string values, not its member names.
    status          text NOT NULL DEFAULT 'submitted'
                        CHECK (status IN ('submitted', 'shortlisted', 'rejected', 'interview_linked')),
    matching_score  jsonb,
    session_id      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz,

    -- The one hard duplicate-prevention rule the current backend enforces
    -- (ApplicationService.apply's get_for_job_and_candidate check) -
    -- promoted from a service-layer check-then-insert (which races across
    -- multiple processes) to a real constraint.
    CONSTRAINT uq_applications_job_candidate UNIQUE (job_id, candidate_id)
);

-- Backs ApplicationRepository.list_for_job.
CREATE INDEX IF NOT EXISTS idx_applications_job_id_created_at
    ON applications (job_id, created_at DESC);
-- Backs ApplicationRepository.list_for_candidate.
CREATE INDEX IF NOT EXISTS idx_applications_candidate_id_created_at
    ON applications (candidate_id, created_at DESC);
-- Backs the shortlist/leaderboard candidate pool (status IN
-- ('shortlisted', 'interview_linked') for one job).
CREATE INDEX IF NOT EXISTS idx_applications_job_id_status
    ON applications (job_id, status);

-- ---------------------------------------------------------------------
-- sessions — SessionRecord (repositories/interfaces.py): the durable,
-- restorable interview session, including the FULL InterviewState (P3
-- adaptive engine state) and the point-in-time job/resume snapshots
-- captured when the interview started.
--
-- `job_description_snapshot` / `parsed_resume_snapshot` are NOT foreign
-- keys to jobs/candidates - they are frozen copies. This is the single
-- most important design constraint from the persistence audit: the
-- backend has never re-read JobDescription/ParsedResume after an
-- interview's runner was constructed (see
-- repositories/interfaces.py's module docstring and
-- services/interview_service.py:_restore_runner /
-- services/evaluation_service.py), so a live FK here would let editing a
-- job or candidate profile after an interview began retroactively change
-- what an in-progress or already-completed interview is scored against.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    session_id                  text PRIMARY KEY,
    -- Nullable: SessionRecord.interview_id is Optional - a record built by
    -- hand with only the original Chunk 1 fields (several existing tests
    -- still do this) has no interview_id yet.
    interview_id                text,
    candidate_id                text NOT NULL REFERENCES candidates (candidate_id),
    job_id                      text NOT NULL REFERENCES jobs (job_id),
    application_id              text REFERENCES applications (application_id),
    -- Exact SessionStatus values (utils/interview_session.py).
    status                      text NOT NULL DEFAULT 'created'
                                    CHECK (status IN ('created', 'active', 'finishing', 'sealed', 'failed')),
    termination_reason          text,
    questions_asked             integer NOT NULL DEFAULT 0,
    questions_answered          integer NOT NULL DEFAULT 0,
    state                       jsonb,
    job_description_snapshot    jsonb,
    parsed_resume_snapshot      jsonb,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz,

    -- Nothing structurally prevented an interview_id collision before this
    -- (the audit's own finding) - cheap insurance since the id is always
    -- uuid4-derived. Postgres treats multiple NULLs as distinct, so this
    -- does not conflict with interview_id being nullable above.
    CONSTRAINT uq_sessions_interview_id UNIQUE (interview_id)
);

-- Backs SessionRepository.list_for_candidate.
CREATE INDEX IF NOT EXISTS idx_sessions_candidate_id_created_at
    ON sessions (candidate_id, created_at DESC);

-- Resolve the circular Application<->Session reference now that both
-- tables exist. Guarded so this file stays safely re-runnable.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_applications_session'
    ) THEN
        ALTER TABLE applications
            ADD CONSTRAINT fk_applications_session
            FOREIGN KEY (session_id) REFERENCES sessions (session_id);
    END IF;
END $$;

-- ---------------------------------------------------------------------
-- transcripts — InterviewTranscript (schemas/interview.py), used as-is
-- with no separate persistence representation. Only ever inserted once
-- sealed - TranscriptRepository.save's contract, mirrored here by the
-- is_sealed CHECK so a direct-DB write cannot bypass it either.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transcripts (
    interview_id      text PRIMARY KEY REFERENCES sessions (interview_id),
    candidate_id       text NOT NULL REFERENCES candidates (candidate_id),
    job_id             text NOT NULL REFERENCES jobs (job_id),
    -- ISO-8601 strings, not timestamptz: the domain layer
    -- (InterviewTranscript) stores these as strings, not datetimes -
    -- preserved as-is per "do not change domain semantics".
    start_time         text NOT NULL,
    end_time           text,
    duration_seconds   integer,
    exchanges          jsonb NOT NULL DEFAULT '[]'::jsonb,
    interviewer_name   text,
    interview_type     text,
    format             text,
    is_sealed          boolean NOT NULL DEFAULT true CHECK (is_sealed = true),
    seal_timestamp     text,
    raw_transcript     text
);

-- Backs TranscriptRepository.get_for_candidate(candidate_id, job_id).
CREATE INDEX IF NOT EXISTS idx_transcripts_candidate_job
    ON transcripts (candidate_id, job_id);

-- ---------------------------------------------------------------------
-- evaluations — EvaluationJob (repositories/interfaces.py), including the
-- completed CandidateReport (schemas/scoring.py) as `result`. No
-- per-agent tables: the audit confirmed intermediate agent outputs
-- (TechnicalEvaluation, BehavioralEvaluation, IntegrityEvaluation,
-- BiasAudit) are not persisted anywhere in the current backend - only
-- what survives into CandidateReport is stored, exactly as today.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id    text PRIMARY KEY,
    -- UNIQUE: the one hard atomicity requirement the current backend
    -- states explicitly (create_if_absent_for_session) - replaces the
    -- in-memory implementation's single-process lock, which does not
    -- survive multiple backend processes.
    session_id       text NOT NULL UNIQUE REFERENCES sessions (session_id),
    interview_id     text NOT NULL REFERENCES sessions (interview_id),
    candidate_id     text NOT NULL REFERENCES candidates (candidate_id),
    job_id           text NOT NULL REFERENCES jobs (job_id),
    -- Nullable: EvaluationJob.application_id is None for a session created
    -- without one (the original /sessions contract, still supported).
    application_id   text REFERENCES applications (application_id),
    -- Exact EvaluationStatus values (repositories/interfaces.py).
    status           text NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    result           jsonb,
    error            text,
    warnings         jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz,
    started_at       timestamptz,
    completed_at     timestamptz,

    -- Mirrors the code's own invariant: _finish_completed always sets
    -- status=COMPLETED and result together (evaluation_service.py).
    CONSTRAINT ck_evaluations_completed_has_result
        CHECK (status <> 'completed' OR result IS NOT NULL)
);

-- Backs the leaderboard's "completed evaluations for this job" pool, and
-- get_many_for_sessions via the session_id UNIQUE index above.
CREATE INDEX IF NOT EXISTS idx_evaluations_job_id_status
    ON evaluations (job_id, status);

COMMIT;

-- ============================================================================
-- Evidence-bound matching: versioned rubrics and cited competency scores.
-- ============================================================================

-- At most one APPROVED row per job_id, enforced by the partial unique index
-- below: a leaderboard whose rows were scored under different rubric versions
-- has incomparable ranks.
CREATE TABLE IF NOT EXISTS job_rubrics (
    rubric_id    TEXT PRIMARY KEY,
    job_id       TEXT NOT NULL,
    version      INTEGER NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('draft', 'approved', 'superseded')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at  TIMESTAMPTZ,
    approved_by  TEXT,
    UNIQUE (job_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_approved_rubric_per_job
    ON job_rubrics (job_id) WHERE status = 'approved';

CREATE TABLE IF NOT EXISTS rubric_competencies (
    competency_id TEXT PRIMARY KEY,
    rubric_id     TEXT NOT NULL REFERENCES job_rubrics(rubric_id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    definition    TEXT NOT NULL,
    weight        DOUBLE PRECISION NOT NULL CHECK (weight >= 0 AND weight <= 1),
    anchor_1      TEXT NOT NULL,
    anchor_2      TEXT NOT NULL,
    anchor_3      TEXT NOT NULL,
    anchor_4      TEXT NOT NULL,
    anchor_5      TEXT NOT NULL,
    UNIQUE (rubric_id, name)
);

-- Verbatim citable resume fragments. Retained because leaderboard rows render
-- the quoted text behind every score; a citation whose span was discarded is
-- unauditable.
CREATE TABLE IF NOT EXISTS resume_spans (
    span_id        TEXT NOT NULL,
    application_id TEXT NOT NULL,
    span_type      TEXT NOT NULL,
    text           TEXT NOT NULL,
    PRIMARY KEY (application_id, span_id)
);

CREATE TABLE IF NOT EXISTS competency_scores (
    score_id             TEXT PRIMARY KEY,
    application_id       TEXT NOT NULL,
    rubric_version       INTEGER NOT NULL,
    competency_name      TEXT NOT NULL,
    score                INTEGER CHECK (score BETWEEN 1 AND 5),
    evidence_sufficiency TEXT NOT NULL
        CHECK (evidence_sufficiency IN ('sufficient', 'partial', 'insufficient')),
    rationale            TEXT NOT NULL,
    UNIQUE (application_id, rubric_version, competency_name)
);

CREATE TABLE IF NOT EXISTS competency_score_citations (
    score_id       TEXT NOT NULL REFERENCES competency_scores(score_id) ON DELETE CASCADE,
    application_id TEXT NOT NULL,
    span_id        TEXT NOT NULL,
    PRIMARY KEY (score_id, span_id),
    FOREIGN KEY (application_id, span_id) REFERENCES resume_spans(application_id, span_id)
);
