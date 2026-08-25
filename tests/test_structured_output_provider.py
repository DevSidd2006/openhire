"""P1 tests: provider-level structured output behavior (mock + OpenAI),
retry/timeout interaction, and the provider-validation vs schema-validation
boundary (Step 7). Never makes a real network call - the OpenAI client is
always a MagicMock/fake, and real openai SDK exception classes are
constructed directly (not raised over a socket) purely to test
classification."""
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai as openai_sdk

from providers.llm.mock import MockLLMProvider
from providers.llm.openai import OpenAIProvider
from providers.base import LLMTransientError, LLMPermanentError
from schemas.llm_outputs import TechnicalEvaluationResult, JDAnalysisResult
from agents.base import BaseAgent
from tests.fakes import ScriptedLLMProvider

TECH_PROMPT = "# Technical Evaluator Agent Prompt\n...transcript..."
JD_PROMPT = "# JD Analyzer Agent Prompt\n...job..."


class _ConcreteAgent(BaseAgent):
    async def execute(self, **kwargs):
        return {}


def _fake_openai_provider(client) -> OpenAIProvider:
    """Build an OpenAIProvider with a fake client, bypassing __init__ (which
    would otherwise construct a real AsyncOpenAI client) - no network access,
    no API key."""
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.api_key = "test-key-not-real"
    provider.model = "gpt-4-turbo"
    provider.client = client
    return provider


class TestMockProviderStructuredOutput:
    """Step 9 provider tests #1-2: mock generate_structured returns valid,
    schema-conformant data; an unmatched prompt's best-effort fallback is
    correctly NOT guaranteed schema-valid (the provider/schema-validation
    boundary from Step 7)."""

    @pytest.mark.asyncio
    async def test_mock_generate_structured_returns_schema_valid_technical_evaluation(self):
        provider = MockLLMProvider()
        raw = await provider.generate_structured(
            TECH_PROMPT, schema=TechnicalEvaluationResult.model_json_schema()
        )
        validated = TechnicalEvaluationResult.model_validate(raw)
        assert validated.technical_score == 8.0
        assert "Python" in validated.competency_scores

    @pytest.mark.asyncio
    async def test_mock_generate_structured_returns_schema_valid_jd_analysis(self):
        provider = MockLLMProvider()
        raw = await provider.generate_structured(JD_PROMPT, schema=JDAnalysisResult.model_json_schema())
        validated = JDAnalysisResult.model_validate(raw)
        assert validated.title
        assert len(validated.competencies) > 0

    @pytest.mark.asyncio
    async def test_mock_unmatched_prompt_fallback_is_not_guaranteed_schema_valid(self):
        """A prompt matching none of the mock's known dispatch keys hits the
        non-JSON 'Mock response to prompt' fallback, so generate_structured
        falls back to _create_mock_structure(schema) - a best-effort shape,
        not guaranteed to satisfy a strict Pydantic model with required
        fields. The PROVIDER only promises "matches the requested shape
        loosely"; agent-level Pydantic validation is what actually enforces
        correctness (Step 7)."""
        provider = MockLLMProvider()
        raw = await provider.generate_structured(
            "this prompt matches no known agent template",
            schema=TechnicalEvaluationResult.model_json_schema(),
        )
        with pytest.raises(Exception):
            TechnicalEvaluationResult.model_validate(raw)


class TestOpenAIProviderStructuredOutputErrorHandling:
    """Step 9 provider tests #3-5: empty/malformed responses are detected,
    provider errors are surfaced (never swallowed). All calls use a fake
    OpenAI client - never a real API call."""

    @pytest.mark.asyncio
    async def test_empty_choices_is_transient(self):
        response = MagicMock(choices=[])
        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=response)
        provider = _fake_openai_provider(client)

        with pytest.raises(LLMTransientError, match="no choices"):
            await provider.generate_structured("prompt", schema={"type": "object"})

    @pytest.mark.asyncio
    async def test_empty_content_is_transient(self):
        choice = MagicMock(finish_reason="stop", message=MagicMock(content=""))
        response = MagicMock(choices=[choice])
        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=response)
        provider = _fake_openai_provider(client)

        with pytest.raises(LLMTransientError, match="empty content"):
            await provider.generate_structured("prompt", schema={"type": "object"})

    @pytest.mark.asyncio
    async def test_truncated_response_is_transient(self):
        choice = MagicMock(finish_reason="length", message=MagicMock(content='{"a": 1'))
        response = MagicMock(choices=[choice])
        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=response)
        provider = _fake_openai_provider(client)

        with pytest.raises(LLMTransientError, match="truncated"):
            await provider.generate_structured("prompt", schema={"type": "object"})

    @pytest.mark.asyncio
    async def test_malformed_json_content_is_transient(self):
        choice = MagicMock(finish_reason="stop", message=MagicMock(content="not valid json {"))
        response = MagicMock(choices=[choice])
        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=response)
        provider = _fake_openai_provider(client)

        with pytest.raises(LLMTransientError, match="malformed"):
            await provider.generate_structured("prompt", schema={"type": "object"})

    @pytest.mark.asyncio
    async def test_valid_response_returns_parsed_dict(self):
        choice = MagicMock(finish_reason="stop", message=MagicMock(content='{"technical_score": 7.0}'))
        response = MagicMock(choices=[choice])
        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=response)
        provider = _fake_openai_provider(client)

        result = await provider.generate_structured("prompt", schema={"type": "object"})
        assert result == {"technical_score": 7.0}

    @pytest.mark.asyncio
    async def test_rate_limit_error_is_classified_transient(self):
        req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        resp = httpx.Response(429, request=req)
        rate_err = openai_sdk.RateLimitError("rate limited", response=resp, body=None)

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=rate_err)
        provider = _fake_openai_provider(client)

        with pytest.raises(LLMTransientError):
            await provider.generate_structured("prompt", schema={"type": "object"})

    @pytest.mark.asyncio
    async def test_authentication_error_is_classified_permanent_not_retried(self):
        req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        resp = httpx.Response(401, request=req)
        auth_err = openai_sdk.AuthenticationError("invalid api key", response=resp, body=None)

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=auth_err)
        provider = _fake_openai_provider(client)

        with pytest.raises(LLMPermanentError):
            await provider.generate_structured("prompt", schema={"type": "object"})

    @pytest.mark.asyncio
    async def test_unclassified_provider_error_defaults_to_permanent(self):
        """An error we don't recognize defaults to permanent (fail fast)
        rather than being blindly retried - safer than assuming it's
        transient (see OpenAIProvider._classify)."""
        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=RuntimeError("something unexpected"))
        provider = _fake_openai_provider(client)

        with pytest.raises(LLMPermanentError):
            await provider.generate_structured("prompt", schema={"type": "object"})


class TestStructuredRetryAndValidation:
    """Step 9 provider tests #6-7 + STEP 8/12 policy: retry works with
    transient structured-output and schema-validation failures; timeout
    works with structured generation; repeated failure is explicit, never
    fake data."""

    @pytest.mark.asyncio
    async def test_validation_failure_is_retried_then_succeeds(self):
        """A structurally-parseable-but-schema-invalid dict on the first
        attempt (score out of [0,10] range), valid on the second - the SAME
        bounded retry budget as provider failures handles this, not a
        separate/unbounded loop."""
        bad = json.dumps({"technical_score": 999.0})  # out of range -> ValidationError
        good = json.dumps({"technical_score": 7.5, "explanation": "ok"})
        fake = ScriptedLLMProvider(script=[bad, good])
        agent = _ConcreteAgent(name="test", llm_provider=fake)

        result = await agent.call_llm_structured(
            "prompt",
            schema=TechnicalEvaluationResult.model_json_schema(),
            validate=TechnicalEvaluationResult.model_validate,
            max_retries=3,
            timeout_seconds=1.0,
        )
        assert isinstance(result, TechnicalEvaluationResult)
        assert result.technical_score == 7.5
        assert len(fake.calls) == 2

    @pytest.mark.asyncio
    async def test_repeated_validation_failure_fails_explicitly_not_fake_data(self):
        bad = json.dumps({"technical_score": 999.0})
        fake = ScriptedLLMProvider(script=[bad])
        agent = _ConcreteAgent(name="test", llm_provider=fake)

        with pytest.raises(RuntimeError, match="failed after 3 attempt"):
            await agent.call_llm_structured(
                "prompt",
                schema=TechnicalEvaluationResult.model_json_schema(),
                validate=TechnicalEvaluationResult.model_validate,
                max_retries=3,
                timeout_seconds=1.0,
            )
        assert len(fake.calls) == 3

    @pytest.mark.asyncio
    async def test_without_validate_raw_dict_is_returned_unchanged(self):
        """Backward compatibility: call_llm_structured without `validate`
        behaves exactly as before P1 - just the raw dict."""
        fake = ScriptedLLMProvider(script=[json.dumps({"a": 1})])
        agent = _ConcreteAgent(name="test", llm_provider=fake)
        result = await agent.call_llm_structured(
            "prompt", schema={"type": "object"}, max_retries=1, timeout_seconds=1.0
        )
        assert result == {"a": 1}

    @pytest.mark.asyncio
    async def test_timeout_works_with_structured_generation(self):
        fake = ScriptedLLMProvider(script=["never used"], hang_seconds=5.0)
        agent = _ConcreteAgent(name="test", llm_provider=fake)

        start = time.monotonic()
        with pytest.raises(RuntimeError, match="failed after 2 attempt"):
            await agent.call_llm_structured(
                "prompt", schema={"type": "object"}, max_retries=2, timeout_seconds=0.05
            )
        elapsed = time.monotonic() - start
        assert elapsed < 3.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
