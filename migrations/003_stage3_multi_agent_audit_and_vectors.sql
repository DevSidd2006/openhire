-- OpenHire Stage 3 Database Schema Migration
-- Migration: 003_stage3_multi_agent_audit_and_vectors.sql
-- Complete Multi-Agent System schema: async agent execution queue, audit trails, and pgvector embeddings.

-- Enable pgvector if available
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================================
-- 1. Pipeline Execution Runs
-- ============================================================================
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id VARCHAR(255) PRIMARY KEY,
    job_id VARCHAR(255) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    stage VARCHAR(50) NOT NULL DEFAULT 'pre_interview', -- pre_interview, interview, post_interview, complete
    status VARCHAR(50) NOT NULL DEFAULT 'pending', -- pending, running, success, failed
    candidate_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    start_time TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    end_time TIMESTAMPTZ,
    duration_seconds NUMERIC(10, 3),
    total_agents_executed INTEGER NOT NULL DEFAULT 0,
    successful_agents INTEGER NOT NULL DEFAULT 0,
    failed_agents INTEGER NOT NULL DEFAULT 0,
    errors JSONB NOT NULL DEFAULT '[]'::jsonb,
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_job_id ON pipeline_runs(job_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON pipeline_runs(status);

-- ============================================================================
-- 2. Agent Audit Logs
-- ============================================================================
CREATE TABLE IF NOT EXISTS agent_audit_logs (
    log_id VARCHAR(255) PRIMARY KEY,
    run_id VARCHAR(255) NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    agent_name VARCHAR(100) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'pending', -- pending, running, success, failed, timeout
    input_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
    output_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    model_name VARCHAR(100),
    provider VARCHAR(100),
    attempt_number INTEGER NOT NULL DEFAULT 1,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    error_message TEXT,
    start_time TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    end_time TIMESTAMPTZ,
    duration_seconds NUMERIC(10, 3)
);

CREATE INDEX IF NOT EXISTS idx_agent_audit_logs_run_id ON agent_audit_logs(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_audit_logs_agent ON agent_audit_logs(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_audit_logs_status ON agent_audit_logs(status);

-- ============================================================================
-- 3. Document Embeddings (Vector Store for JDs & Resumes)
-- ============================================================================
CREATE TABLE IF NOT EXISTS document_embeddings (
    id VARCHAR(255) PRIMARY KEY,
    doc_id VARCHAR(255) NOT NULL,
    doc_type VARCHAR(50) NOT NULL, -- "job", "resume", "rubric", "question"
    chunk_index INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding vector(384), -- 384 dimensions (all-MiniLM-L6-v2 default)
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_doc_embeddings_doc_type ON document_embeddings(doc_type, doc_id);

-- ============================================================================
-- 4. Async Task Queue (for Evaluation Agent Fan-out)
-- ============================================================================
CREATE TABLE IF NOT EXISTS async_agent_tasks (
    task_id VARCHAR(255) PRIMARY KEY,
    evaluation_id VARCHAR(255) NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
    agent_name VARCHAR(100) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'queued', -- queued, processing, completed, failed
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_async_tasks_eval ON async_agent_tasks(evaluation_id);
CREATE INDEX IF NOT EXISTS idx_async_tasks_status ON async_agent_tasks(status, scheduled_at);
