from pathlib import Path


def test_existing_credential_migration_preserves_rows_and_adds_multi_key_storage():
    migration = Path("repositories/postgres/migrations/20261102_byok_multi_credentials.sql").read_text(encoding="utf-8").lower()
    assert migration.strip().startswith("begin;")
    assert migration.strip().endswith("commit;")
    assert "update user_llm_credentials set credential_id" in migration
    assert "where credential_id is null" in migration
    assert "drop table" not in migration
    assert "delete from user_llm_credentials" not in migration
    assert "create table if not exists user_llm_settings" in migration
    assert "create table if not exists user_llm_usage" in migration
    assert "primary key (credential_id)" in migration
    assert "add column if not exists provider text" in migration
    assert "alter column mode set default 'platform'" in migration
