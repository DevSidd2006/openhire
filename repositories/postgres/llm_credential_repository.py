"""PostgreSQL persistence for recruiter-owned LLM keys and preferences."""
from __future__ import annotations
from typing import Optional
import asyncpg
from repositories.interfaces import CredentialStatus, LLMCredentialRecord, LLMCredentialRepository
from repositories.postgres.pool import PostgresConnectionPool


def _credential_from_row(row: asyncpg.Record) -> LLMCredentialRecord:
    return LLMCredentialRecord(
        user_id=row["user_id"], credential_id=row["credential_id"], name=row["name"],
        provider=row["provider"], model=row["model"], encrypted_key=bytes(row["encrypted_key"]),
        key_hint=row["key_hint"], status=CredentialStatus(row["status"]),
        last_error=row["last_error"], last_error_at=row["last_error_at"],
        health_status=row.get("health_status", "not_tested"), auth_status=row.get("auth_status", "unknown"),
        last_tested_at=row.get("last_tested_at"), last_successful_test_at=row.get("last_successful_test_at"),
        health_error_code=row.get("health_error_code"),
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


class PostgresLLMCredentialRepository(LLMCredentialRepository):
    def __init__(self, pool: PostgresConnectionPool) -> None:
        self._pool = pool

    async def save(self, record: LLMCredentialRecord) -> LLMCredentialRecord:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO user_llm_credentials
                  (credential_id,user_id,name,provider,model,encrypted_key,key_hint,status,last_error,last_error_at,health_status,auth_status,last_tested_at,last_successful_test_at,health_error_code,created_at,updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,now())
                ON CONFLICT (credential_id) DO UPDATE SET
                  name=EXCLUDED.name,provider=EXCLUDED.provider,model=EXCLUDED.model,
                  encrypted_key=EXCLUDED.encrypted_key,key_hint=EXCLUDED.key_hint,status=EXCLUDED.status,
                  last_error=EXCLUDED.last_error,last_error_at=EXCLUDED.last_error_at,health_status=EXCLUDED.health_status,
                  auth_status=EXCLUDED.auth_status,last_tested_at=EXCLUDED.last_tested_at,last_successful_test_at=EXCLUDED.last_successful_test_at,
                  health_error_code=EXCLUDED.health_error_code,updated_at=now()
                WHERE user_llm_credentials.user_id=EXCLUDED.user_id
                RETURNING *
            """, record.credential_id,record.user_id,record.name,record.provider,record.model,record.encrypted_key,
                record.key_hint,record.status.value,record.last_error,record.last_error_at,record.health_status,record.auth_status,
                record.last_tested_at,record.last_successful_test_at,record.health_error_code,record.created_at)
        if row is None:
            raise ValueError("Credential ownership mismatch")
        return _credential_from_row(row)

    async def get(self, user_id: str) -> Optional[LLMCredentialRecord]:
        rows = await self.list_for_user(user_id)
        if not rows: return None
        settings = await self.get_settings(user_id)
        return next((r for r in rows if r.credential_id == settings.get("default_credential_id")), rows[0])

    async def list_for_user(self, user_id: str) -> list[LLMCredentialRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM user_llm_credentials WHERE user_id=$1 ORDER BY created_at,credential_id", user_id)
        return [_credential_from_row(r) for r in rows]

    async def get_by_id(self, user_id: str, credential_id: str) -> Optional[LLMCredentialRecord]:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM user_llm_credentials WHERE user_id=$1 AND credential_id=$2", user_id, credential_id)
        return _credential_from_row(row) if row else None

    async def delete(self, user_id: str) -> None:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM user_llm_credentials WHERE user_id=$1", user_id)

    async def delete_key(self, user_id: str, credential_id: str) -> None:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM user_llm_credentials WHERE user_id=$1 AND credential_id=$2", user_id, credential_id)

    async def get_settings(self, user_id: str) -> dict:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT mode,default_credential_id,provider,fallback_to_other_keys,fallback_to_platform,model FROM user_llm_settings WHERE user_id=$1", user_id)
        return dict(row) if row else {"mode":"platform","default_credential_id":None,"provider":None,"fallback_to_other_keys":True,"fallback_to_platform":True,"model":None}

    async def save_settings(self, user_id: str, settings: dict) -> dict:
        pool = await self._pool.get()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO user_llm_settings(user_id,mode,default_credential_id,provider,fallback_to_other_keys,fallback_to_platform,model,updated_at)
                VALUES($1,$2,$3,$4,$5,$6,$7,now()) ON CONFLICT(user_id) DO UPDATE SET
                mode=EXCLUDED.mode,default_credential_id=EXCLUDED.default_credential_id,
                provider=EXCLUDED.provider,
                fallback_to_other_keys=EXCLUDED.fallback_to_other_keys,fallback_to_platform=EXCLUDED.fallback_to_platform,
                model=EXCLUDED.model,updated_at=now()
                RETURNING mode,default_credential_id,provider,fallback_to_other_keys,fallback_to_platform,model
            """, user_id,settings["mode"],settings.get("default_credential_id"),settings.get("provider"),settings["fallback_to_other_keys"],settings["fallback_to_platform"],settings.get("model"))
        return dict(row)

    async def record_usage(self, user_id: str, credential_id: str, provider: str, outcome: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        pool=await self._pool.get()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_llm_usage(user_id,credential_id,provider,requests,successes,failures,input_tokens,output_tokens,updated_at)
                VALUES($1,$2,$3,1,$4,$5,$6,$7,now()) ON CONFLICT(user_id,credential_id,provider) DO UPDATE SET
                requests=user_llm_usage.requests+1,successes=user_llm_usage.successes+EXCLUDED.successes,
                failures=user_llm_usage.failures+EXCLUDED.failures,input_tokens=user_llm_usage.input_tokens+EXCLUDED.input_tokens,
                output_tokens=user_llm_usage.output_tokens+EXCLUDED.output_tokens,updated_at=now()
            """,user_id,credential_id,provider,1 if outcome=="success" else 0,1 if outcome!="success" else 0,input_tokens,output_tokens)

    async def get_usage(self, user_id: str) -> list[dict]:
        pool=await self._pool.get()
        async with pool.acquire() as conn:
            rows=await conn.fetch("SELECT * FROM user_llm_usage WHERE user_id=$1 ORDER BY provider,credential_id",user_id)
        return [dict(r) for r in rows]

    async def save_platform_health(self, provider: str, result: dict) -> None:
        pool=await self._pool.get()
        async with pool.acquire() as conn:
            await conn.execute("""INSERT INTO platform_llm_provider_health(provider,model,status,auth_status,last_tested_at,last_successful_test_at,error_code,message,updated_at)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,now()) ON CONFLICT(provider) DO UPDATE SET model=EXCLUDED.model,status=EXCLUDED.status,
                auth_status=EXCLUDED.auth_status,last_tested_at=EXCLUDED.last_tested_at,last_successful_test_at=COALESCE(EXCLUDED.last_successful_test_at,platform_llm_provider_health.last_successful_test_at),
                error_code=EXCLUDED.error_code,message=EXCLUDED.message,updated_at=now()""", provider,result.get("model"),result.get("status"),result.get("auth_status"),
                result.get("last_tested_at"),result.get("last_successful_test_at"),result.get("error_code"),result.get("message"))

    async def get_platform_health(self) -> list[dict]:
        pool=await self._pool.get()
        async with pool.acquire() as conn:
            rows=await conn.fetch("SELECT provider,model,status,auth_status,last_tested_at,last_successful_test_at,error_code,message FROM platform_llm_provider_health ORDER BY provider")
        return [dict(r) for r in rows]
