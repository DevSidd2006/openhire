"""Tests verifying PostgreSQL schema and migration scripts for Stage 1."""
from pathlib import Path

def test_migration_file_exists_and_contains_stage1_tables():
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    migration_file = migrations_dir / "001_initial_schema.sql"
    
    assert migration_file.exists(), "001_initial_schema.sql must exist"
    content = migration_file.read_text(encoding="utf-8").lower()
    
    # Required Stage 1 tables according to docs/roles/database.md
    expected_tables = ["jobs", "candidates", "interviews", "transcripts", "evaluations", "applications"]
    for table in expected_tables:
        assert f"create table if not exists {table}" in content or f"create table {table}" in content, f"Table {table} missing in schema"

    # Stage 1 column checks
    assert "questions jsonb" in content
    assert "status varchar" in content
    assert "created_at timestamptz" in content

def test_stage2_migration_file_exists_and_contains_rubric_tables():
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    migration_file = migrations_dir / "002_stage2_rubrics_and_competencies.sql"
    
    assert migration_file.exists(), "002_stage2_rubrics_and_competencies.sql must exist"
    content = migration_file.read_text(encoding="utf-8").lower()
    
    expected_tables = ["job_rubrics", "candidate_competency_scores", "job_leaderboards"]
    for table in expected_tables:
        assert f"create table if not exists {table}" in content or f"create table {table}" in content, f"Table {table} missing in Stage 2 schema"
    assert "competencies jsonb" in content
    assert "pass_threshold numeric" in content

def test_stage3_migration_file_exists_and_contains_agent_tables():
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    migration_file = migrations_dir / "003_stage3_multi_agent_audit_and_vectors.sql"
    
    assert migration_file.exists(), "003_stage3_multi_agent_audit_and_vectors.sql must exist"
    content = migration_file.read_text(encoding="utf-8").lower()
    
    expected_tables = ["pipeline_runs", "agent_audit_logs", "document_embeddings", "async_agent_tasks"]
    for table in expected_tables:
        assert f"create table if not exists {table}" in content or f"create table {table}" in content, f"Table {table} missing in Stage 3 schema"
    assert "agent_name varchar" in content
    assert "document_embeddings" in content

def test_stage4_migration_file_exists_and_contains_production_tables():
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    migration_file = migrations_dir / "004_stage4_production_compliance_and_billing.sql"
    
    assert migration_file.exists(), "004_stage4_production_compliance_and_billing.sql must exist"
    content = migration_file.read_text(encoding="utf-8").lower()
    
    expected_tables = [
        "organizations", "organization_members", "human_review_decisions",
        "credit_transactions", "integration_delivery_logs", "interview_connectivity_telemetry"
    ]
    for table in expected_tables:
        assert f"create table if not exists {table}" in content or f"create table {table}" in content, f"Table {table} missing in Stage 4 schema"
    assert "inr_credits_balance" in content
    assert "human_review_decisions" in content
