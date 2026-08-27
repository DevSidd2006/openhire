-- OpenHire Stage 2 Database Schema Migration
-- Migration: 002_stage2_rubrics_and_competencies.sql
-- Adds support for multi-competency rubrics, structured score breakdowns, and interview configuration.

-- ============================================================================
-- 1. Job Rubrics Table
-- ============================================================================
CREATE TABLE IF NOT EXISTS job_rubrics (
    id VARCHAR(255) PRIMARY KEY,
    job_id VARCHAR(255) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    interview_type VARCHAR(50) NOT NULL DEFAULT 'technical', -- technical, hr, behavioral, mixed
    competencies JSONB NOT NULL DEFAULT '[]'::jsonb, -- array of Competency (name, weight, level, importance)
    pass_threshold NUMERIC(4, 2) DEFAULT 6.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ,
    CONSTRAINT uq_job_rubrics_job_type UNIQUE (job_id, interview_type)
);

CREATE INDEX IF NOT EXISTS idx_job_rubrics_job_id ON job_rubrics(job_id);

-- ============================================================================
-- 2. Competency Scores Breakdown Table
-- ============================================================================
CREATE TABLE IF NOT EXISTS candidate_competency_scores (
    id VARCHAR(255) PRIMARY KEY,
    evaluation_id VARCHAR(255) NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
    candidate_id VARCHAR(255) NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    job_id VARCHAR(255) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    competency VARCHAR(255) NOT NULL,
    weight NUMERIC(4, 2) NOT NULL DEFAULT 1.0,
    score NUMERIC(4, 2) NOT NULL,
    confidence NUMERIC(4, 2) DEFAULT 1.0,
    evidence_count INTEGER DEFAULT 0,
    explanation TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_comp_scores_evaluation_id ON candidate_competency_scores(evaluation_id);
CREATE INDEX IF NOT EXISTS idx_comp_scores_candidate_job ON candidate_competency_scores(candidate_id, job_id);
CREATE INDEX IF NOT EXISTS idx_comp_scores_competency ON candidate_competency_scores(competency);

-- ============================================================================
-- 3. Leaderboard Cache Table
-- ============================================================================
CREATE TABLE IF NOT EXISTS job_leaderboards (
    id VARCHAR(255) PRIMARY KEY,
    job_id VARCHAR(255) UNIQUE NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ranked_entries JSONB NOT NULL DEFAULT '[]'::jsonb, -- array of LeaderboardEntry
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_job_leaderboards_job_id ON job_leaderboards(job_id);
