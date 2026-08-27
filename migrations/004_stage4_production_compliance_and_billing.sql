-- OpenHire Stage 4 Database Schema Migration
-- Migration: 004_stage4_production_compliance_and_billing.sql
-- Production Scale, India Data Residency & Compliance, ATS/WhatsApp Sync, Credits & Human Review Gate

-- ============================================================================
-- 1. Organizations & Recruiter Multi-Tenancy (RBAC)
-- ============================================================================
CREATE TABLE IF NOT EXISTS organizations (
    id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,
    inr_credits_balance NUMERIC(10, 2) NOT NULL DEFAULT 0.00,
    data_residency_region VARCHAR(50) NOT NULL DEFAULT 'ap-south-1', -- Mumbai / India Only
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS organization_members (
    id VARCHAR(255) PRIMARY KEY,
    org_id VARCHAR(255) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL DEFAULT 'recruiter', -- admin, recruiter, reviewer
    email VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_org_member UNIQUE (org_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_org_members_org ON organization_members(org_id);
CREATE INDEX IF NOT EXISTS idx_org_members_user ON organization_members(user_id);

-- ============================================================================
-- 2. Human Review Gate & Hiring Decisions
-- ============================================================================
CREATE TABLE IF NOT EXISTS human_review_decisions (
    id VARCHAR(255) PRIMARY KEY,
    evaluation_id VARCHAR(255) NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
    candidate_id VARCHAR(255) NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    job_id VARCHAR(255) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    reviewer_id VARCHAR(255) NOT NULL,
    decision VARCHAR(50) NOT NULL, -- 'approved', 'rejected', 're_interview_required', 'hold'
    notes TEXT,
    override_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_human_review_eval ON human_review_decisions(evaluation_id);
CREATE INDEX IF NOT EXISTS idx_human_review_job ON human_review_decisions(job_id);

-- ============================================================================
-- 3. Billing & Per-Interview INR Credits Ledger
-- ============================================================================
CREATE TABLE IF NOT EXISTS credit_transactions (
    id VARCHAR(255) PRIMARY KEY,
    org_id VARCHAR(255) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    amount NUMERIC(10, 2) NOT NULL, -- positive for recharge, negative for deduction
    currency VARCHAR(10) NOT NULL DEFAULT 'INR',
    transaction_type VARCHAR(50) NOT NULL, -- 'recharge', 'interview_charge', 'refund', 'adjustment'
    description TEXT,
    reference_id VARCHAR(255), -- e.g. payment_gateway_id or session_id
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_credit_tx_org ON credit_transactions(org_id);
CREATE INDEX IF NOT EXISTS idx_credit_tx_created ON credit_transactions(created_at DESC);

-- ============================================================================
-- 4. ATS & Messaging Integrations (Naukri, WhatsApp / SMS Delivery)
-- ============================================================================
CREATE TABLE IF NOT EXISTS integration_delivery_logs (
    id VARCHAR(255) PRIMARY KEY,
    job_id VARCHAR(255) REFERENCES jobs(id) ON DELETE SET NULL,
    candidate_id VARCHAR(255) REFERENCES candidates(id) ON DELETE SET NULL,
    channel VARCHAR(50) NOT NULL, -- 'whatsapp', 'sms', 'naukri_ats', 'greenhouse', 'workday'
    direction VARCHAR(20) NOT NULL DEFAULT 'outbound', -- 'outbound', 'inbound'
    status VARCHAR(50) NOT NULL DEFAULT 'sent', -- 'sent', 'delivered', 'failed', 'synced'
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_delivery_logs_candidate ON integration_delivery_logs(candidate_id);
CREATE INDEX IF NOT EXISTS idx_delivery_logs_channel ON integration_delivery_logs(channel, status);

-- ============================================================================
-- 5. Fallback Connectivity & Multilingual Slices
-- ============================================================================
CREATE TABLE IF NOT EXISTS interview_connectivity_telemetry (
    id VARCHAR(255) PRIMARY KEY,
    session_id VARCHAR(255) NOT NULL REFERENCES interviews(session_id) ON DELETE CASCADE,
    connection_mode VARCHAR(50) NOT NULL DEFAULT 'audio_websocket', -- 'video', 'audio_websocket', 'async_upload', 'phone_call'
    packet_loss_pct NUMERIC(5, 2) DEFAULT 0.0,
    latency_ms INTEGER DEFAULT 0,
    detected_language VARCHAR(50) DEFAULT 'en', -- en, hi, ta, te, bn, code_mixed
    fallback_transitions JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_conn_telemetry_session ON interview_connectivity_telemetry(session_id);
