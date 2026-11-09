from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from repositories.postgres.llm_credential_repository import PostgresLLMCredentialRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["success", "failure", "not_configured", "datetime"])
async def test_platform_health_binds_datetime_values(kind):
    stamp = datetime(2026, 11, 6, 12, 30, tzinfo=timezone.utc)
    tested = None if kind == "not_configured" else stamp
    successful = stamp if kind in ("success", "datetime") else None
    result = {
        "model": "gpt-test", "status": "connected" if successful else "connection_error",
        "auth_status": "valid" if successful else "unknown",
        "last_tested_at": tested.isoformat() if tested and kind != "datetime" else tested,
        "last_successful_test_at": successful.isoformat() if successful and kind != "datetime" else successful,
    }
    original = dict(result)
    connection = MagicMock()
    connection.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
    wrapper = MagicMock()
    wrapper.get = AsyncMock(return_value=pool)

    await PostgresLLMCredentialRepository(wrapper).save_platform_health("openai", result)

    args = connection.execute.call_args.args
    assert args[5] == tested
    assert args[6] == successful
    assert result == original
