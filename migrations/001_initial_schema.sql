-- OpenHire Stage 1 Database Schema Migration
-- Migration: 001_initial_schema.sql
-- Aligned with docs/roles/database.md & repositories/interfaces.py

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================================
-- 1. JOBS: jobs(id, title, questions jsonb, created_at)
-- ============================================================================
CREATE TABLE IF NOT EXISTS jobs (
    id VARCHAR(255) PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    questions JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    job_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_jobs_is_active ON jobs(is_active);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);

-- ============================================================================
-- 2. CANDIDATES: candidates(id, name, email, created_at)
-- ============================================================================
CREATE TABLE IF NOT EXISTS candidates (
    id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(255),
    email VARCHAR(255),
    resume JSONB NOT NULL DEFAULT '{}'::jsonb,
    used_fallback BOOLEAN NOT NULL DEFAULT FALSE,
    parse_warning TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_candidates_email ON candidates(email);
CREATE INDEX IF NOT EXISTS idx_candidates_created_at ON candidates(created_at DESC);

-- ============================================================================
-- 3. APPLICATIONS: applications(id, job_id, candidate_id, status, ...)
-- ============================================================================
CREATE TABLE IF NOT EXISTS applications (
    id VARCHAR(255) PRIMARY KEY,
    job_id VARCHAR(255) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    candidate_id VARCHAR(255) NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    status VARCHAR(50) NOT NULL DEFAULT 'applied',
    matching_score JSONB,
    session_id VARCHAR(255),
    evaluation_id VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ,
    CONSTRAINT uq_applications_job_candidate UNIQUE (job_id, candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_applications_job_id ON applications(job_id);
CREATE INDEX IF NOT EXISTS idx_applications_candidate_id ON applications(candidate_id);
CREATE INDEX IF NOT EXISTS idx_applications_session_id ON applications(session_id);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);

-- ============================================================================
-- 4. INTERVIEWS: interviews(id, job_id, candidate_id, link_token, status, created_at)
-- ============================================================================
CREATE TABLE IF NOT EXISTS interviews (
    id VARCHAR(255) PRIMARY KEY,
    session_id VARCHAR(255) UNIQUE NOT NULL,
    job_id VARCHAR(255) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    candidate_id VARCHAR(255) NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    application_id VARCHAR(255) REFERENCES applications(id) ON DELETE SET NULL,
    link_token VARCHAR(255),
    status VARCHAR(50) NOT NULL DEFAULT 'created',
    termination_reason TEXT,
    questions_asked INTEGER NOT NULL DEFAULT 0,
    questions_answered INTEGER NOT NULL DEFAULT 0,
    state JSONB,
    job_description JSONB,
    parsed_resume JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_interviews_session_id ON interviews(session_id);
CREATE INDEX IF NOT EXISTS idx_interviews_candidate_id ON interviews(candidate_id);
CREATE INDEX IF NOT EXISTS idx_interviews_job_id ON interviews(job_id);
CREATE INDEX IF NOT EXISTS idx_interviews_application_id ON interviews(application_id);
CREATE INDEX IF NOT EXISTS idx_interviews_status ON interviews(status);
CREATE INDEX IF NOT EXISTS idx_interviews_link_token ON interviews(link_token);

-- ============================================================================
-- 5. TRANSCRIPTS: transcripts(id, interview_id, question_index, answer_text, audio_url, created_at)
-- ============================================================================
CREATE TABLE IF NOT EXISTS transcripts (
    id VARCHAR(255) PRIMARY KEY,
    interview_id VARCHAR(255) NOT NULL,
    candidate_id VARCHAR(255) NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    job_id VARCHAR(255) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    question_index INTEGER DEFAULT 0,
    answer_text TEXT,
    audio_url TEXT,
    transcript_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_sealed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_transcripts_interview_id ON transcripts(interview_id);
CREATE INDEX IF NOT EXISTS idx_transcripts_candidate_job ON transcripts(candidate_id, job_id);

-- ============================================================================
-- 6. SCORES / EVALUATIONS: scores(id, interview_id, overall_score, report_text, created_at)
-- ============================================================================
CREATE TABLE IF NOT EXISTS evaluations (
    id VARCHAR(255) PRIMARY KEY,
    session_id VARCHAR(255) UNIQUE NOT NULL,
    interview_id VARCHAR(255) NOT NULL,
    candidate_id VARCHAR(255) NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    job_id VARCHAR(255) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    application_id VARCHAR(255) REFERENCES applications(id) ON DELETE SET NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    overall_score NUMERIC(5,2),
    report_text TEXT,
    result JSONB,
    error TEXT,
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_evaluations_session_id ON evaluations(session_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_interview_id ON evaluations(interview_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_status ON evaluations(status);
CREATE INDEX IF NOT EXISTS idx_evaluations_candidate_id ON evaluations(candidate_id);
