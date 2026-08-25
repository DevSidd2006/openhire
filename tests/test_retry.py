"""Tests for P0-6: retry/timeout must actually be wired into LLM calls.
Uses a scripted fake provider only - never a real API call."""
import time
import pytest

from agents.base import BaseAgent
from providers.base import LLMPermanentError, LLMTransientError
from tests.fakes import ScriptedLLMProvider


class _ConcreteAgent(BaseAgent):
    """Minimal concrete BaseAgent subclass; execute() isn't exercised here,
    only the call_llm_generate retry wrapper."""

    async def execute(self, **kwargs):
        return {}


class TestSuccessNoRetry:
    @pytest.mark.asyncio
    async def test_successful_call_does_not_retry(self):
        fake = ScriptedLLMProvider(script=["ok"])
        agent = _ConcreteAgent(name="test", llm_provider=fake)

        result = await agent.call_llm_generate("prompt", max_retries=3, timeout_seconds=1.0)

        assert result == "ok"
        assert len(fake.calls) == 1


class TestTransientRetry:
    @pytest.mark.asyncio
    async def test_transient_failure_is_retried_then_succeeds(self):
        fake = ScriptedLLMProvider(script=[LLMTransientError("rate limited"), "ok"])
        agent = _ConcreteAgent(name="test", llm_provider=fake)

        result = await agent.call_llm_generate("prompt", max_retries=3, timeout_seconds=1.0)

        assert result == "ok"
        assert len(fake.calls) == 2

    @pytest.mark.asyncio
    async def test_repeated_transient_failure_fails_clearly_after_exhausting_retries(self):
        fake = ScriptedLLMProvider(script=[LLMTransientError("still down")])
        agent = _ConcreteAgent(name="test", llm_provider=fake)

        with pytest.raises(RuntimeError, match="failed after 3 attempt"):
            await agent.call_llm_generate("prompt", max_retries=3, timeout_seconds=1.0)

        # Exactly max_retries attempts - no retry storm past the configured limit.
        assert len(fake.calls) == 3


class TestPermanentFailureFailsFast:
    @pytest.mark.asyncio
    async def test_permanent_error_is_not_retried(self):
        fake = ScriptedLLMProvider(script=[LLMPermanentError("invalid api key")])
        agent = _ConcreteAgent(name="test", llm_provider=fake)

        with pytest.raises(LLMPermanentError):
            await agent.call_llm_generate("prompt", max_retries=3, timeout_seconds=1.0)

        # A permanent/config error must fail immediately, not burn retries.
        assert len(fake.calls) == 1


class TestTimeoutHandling:
    @pytest.mark.asyncio
    async def test_hung_call_is_actually_cancelled_by_timeout(self):
        """A provider that never returns in time must not hang the caller
        for the full duration - asyncio.wait_for must actually cut it off."""
        fake = ScriptedLLMProvider(script=["should never be reached"], hang_seconds=5.0)
        agent = _ConcreteAgent(name="test", llm_provider=fake)

        start = time.monotonic()
        with pytest.raises(RuntimeError, match="failed after 2 attempt"):
            await agent.call_llm_generate("prompt", max_retries=2, timeout_seconds=0.05)
        elapsed = time.monotonic() - start

        # 2 attempts at 0.05s timeout + capped backoff, nowhere near the 5s hang.
        assert elapsed < 3.0
        assert len(fake.calls) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
