"""
P8B: Gemini provider tests.

Mirrors tests/test_structured_output_provider.py's approach for OpenAI -
every SDK call is a mocked google.genai client, NEVER a real network call
or API key. The one real Gemini call in this phase is a separate, manual,
single-prompt smoke test (not part of the automated suite) - see the P8B
final report for its result.
"""
import json
import time

import pytest
from unittest.mock import AsyncMock, MagicMock

from google.genai import errors, types

from agents.base import BaseAgent
from providers.base import LLMPermanentError, LLMTransientError
from providers.llm.gemini import GeminiProvider
from schemas.llm_outputs import TechnicalEvaluationResult


class _ConcreteAgent(BaseAgent):
    async def execute(self, **kwargs):
        return {}


def _response(text: str, finish_reason=types.FinishReason.STOP) -> types.GenerateContentResponse:
    """Build a REAL google.genai response object (not a MagicMock) - the
    SDK's own response.text property does real pydantic field access
    (part.model_dump(...)) that a MagicMock can't stand in for correctly."""
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(parts=[types.Part(text=text)], role="model"),
                finish_reason=finish_reason,
            )
        ]
    )


def _empty_response() -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[])


def _fake_gemini_provider(generate_content_mock) -> GeminiProvider:
    """A real GeminiProvider (construction makes no network call - just
    builds a local google.genai.Client object) with its SDK call replaced
    by a mock. No API key ever reaches this test key value; it is a
    hardcoded, obviously-fake placeholder."""
    provider = GeminiProvider(api_key="test-key-not-real", model="gemini-2.5-flash")
    provider.client.aio.models.generate_content = generate_content_mock
    return provider


class TestGeminiProviderConstruction:
    def test_a_constructs_when_credentials_available(self):
        provider = GeminiProvider(api_key="test-key-not-real", model="gemini-2.5-flash")
        assert provider.model == "gemini-2.5-flash"

    def test_b_missing_api_key_is_explicit_configuration_error(self):
        with pytest.raises(LLMPermanentError, match="GEMINI_API_KEY"):
            GeminiProvider(api_key="", model="gemini-2.5-flash")

    def test_b_factory_rejects_gemini_without_api_key(self, monkeypatch):
        import providers.llm as llm_module
        monkeypatch.setattr(llm_module, "LLM_PROVIDER", "gemini")
        monkeypatch.setattr(llm_module, "GEMINI_API_KEY", "")
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            llm_module.get_llm_provider()


class TestGeminiGenerate:
    @pytest.mark.asyncio
    async def test_c_generate_uses_the_configured_model(self):
        mock_call = AsyncMock(return_value=_response("hello world"))
        provider = _fake_gemini_provider(mock_call)
        result = await provider.generate("say hello")
        assert result == "hello world"
        assert mock_call.call_args.kwargs["model"] == "gemini-2.5-flash"
        assert mock_call.call_args.kwargs["contents"] == "say hello"

    @pytest.mark.asyncio
    async def test_empty_content_is_transient(self):
        provider = _fake_gemini_provider(AsyncMock(return_value=_empty_response()))
        with pytest.raises(LLMTransientError, match="empty content"):
            await provider.generate("prompt")

    @pytest.mark.asyncio
    async def test_safety_block_is_permanent_not_retried(self):
        provider = _fake_gemini_provider(
            AsyncMock(return_value=_response("", finish_reason=types.FinishReason.SAFETY))
        )
        with pytest.raises(LLMPermanentError, match="SAFETY"):
            await provider.generate("prompt")

    @pytest.mark.asyncio
    async def test_truncated_response_is_transient(self):
        provider = _fake_gemini_provider(
            AsyncMock(return_value=_response("partial...", finish_reason=types.FinishReason.MAX_TOKENS))
        )
        with pytest.raises(LLMTransientError, match="truncated"):
            await provider.generate("prompt")


class TestGeminiGenerateStructured:
    @pytest.mark.asyncio
    async def test_d_generate_structured_produces_schema_compatible_output(self):
        raw = json.dumps({"technical_score": 8.5, "explanation": "solid"})
        provider = _fake_gemini_provider(AsyncMock(return_value=_response(raw)))

        result = await provider.generate_structured(
            "evaluate this", schema=TechnicalEvaluationResult.model_json_schema(),
        )
        validated = TechnicalEvaluationResult.model_validate(result)
        assert validated.technical_score == 8.5

    @pytest.mark.asyncio
    async def test_d_structured_call_requests_json_mime_and_schema(self):
        mock_call = AsyncMock(return_value=_response(json.dumps({"technical_score": 5.0})))
        provider = _fake_gemini_provider(mock_call)
        schema = TechnicalEvaluationResult.model_json_schema()

        await provider.generate_structured("evaluate this", schema=schema)

        config = mock_call.call_args.kwargs["config"]
        assert config.response_mime_type == "application/json"
        assert config.response_json_schema == schema

    @pytest.mark.asyncio
    async def test_e_malformed_structured_json_is_transient_and_retried(self):
        """Same retry contract as every other provider (P7/P8A): malformed
        JSON text is classified as retryable, not an immediate hard failure."""
        mock_call = AsyncMock(side_effect=[
            _response("not valid json {"),
            _response(json.dumps({"technical_score": 6.0})),
        ])
        provider = _fake_gemini_provider(mock_call)
        agent = _ConcreteAgent(name="test", llm_provider=provider)

        result = await agent.call_llm_structured(
            "prompt", schema=TechnicalEvaluationResult.model_json_schema(),
            validate=TechnicalEvaluationResult.model_validate,
            max_retries=3, timeout_seconds=2.0,
        )
        assert result.technical_score == 6.0
        assert mock_call.call_count == 2

    @pytest.mark.asyncio
    async def test_e_repeated_malformed_json_fails_explicitly(self):
        provider = _fake_gemini_provider(AsyncMock(return_value=_response("still not json")))
        agent = _ConcreteAgent(name="test", llm_provider=provider)

        with pytest.raises(RuntimeError, match=r"failed after 3 attempt"):
            await agent.call_llm_structured(
                "prompt", schema=TechnicalEvaluationResult.model_json_schema(),
                validate=TechnicalEvaluationResult.model_validate,
                max_retries=3, timeout_seconds=2.0,
            )


class TestGeminiErrorClassification:
    @pytest.mark.asyncio
    async def test_f_authentication_error_is_permanent_not_retried(self):
        auth_err = errors.ClientError(
            code=401, response_json={"error": {"message": "API key not valid", "status": "UNAUTHENTICATED", "code": 401}},
        )
        mock_call = AsyncMock(side_effect=auth_err)
        provider = _fake_gemini_provider(mock_call)
        agent = _ConcreteAgent(name="test", llm_provider=provider)

        with pytest.raises(LLMPermanentError):
            await agent.call_llm_generate("prompt", max_retries=3, timeout_seconds=2.0)
        assert mock_call.call_count == 1  # never retried

    @pytest.mark.asyncio
    async def test_f_bad_request_is_permanent(self):
        bad_req = errors.ClientError(
            code=400, response_json={"error": {"message": "invalid argument", "status": "INVALID_ARGUMENT", "code": 400}},
        )
        provider = _fake_gemini_provider(AsyncMock(side_effect=bad_req))
        with pytest.raises(LLMPermanentError):
            await provider.generate("prompt")

    @pytest.mark.asyncio
    async def test_g_rate_limit_is_transient_and_retried(self):
        rate_err = errors.ClientError(
            code=429, response_json={"error": {"message": "rate limited", "status": "RESOURCE_EXHAUSTED", "code": 429}},
        )
        mock_call = AsyncMock(side_effect=[rate_err, _response("ok")])
        provider = _fake_gemini_provider(mock_call)
        agent = _ConcreteAgent(name="test", llm_provider=provider)

        result = await agent.call_llm_generate("prompt", max_retries=3, timeout_seconds=2.0)
        assert result == "ok"
        assert mock_call.call_count == 2

    @pytest.mark.asyncio
    async def test_g_server_error_is_transient_and_bounded_by_max_retries(self):
        server_err = errors.ServerError(
            code=503, response_json={"error": {"message": "unavailable", "status": "UNAVAILABLE", "code": 503}},
        )
        mock_call = AsyncMock(side_effect=server_err)
        provider = _fake_gemini_provider(mock_call)
        agent = _ConcreteAgent(name="test", llm_provider=provider)

        with pytest.raises(RuntimeError, match=r"failed after 3 attempt"):
            await agent.call_llm_generate("prompt", max_retries=3, timeout_seconds=2.0)
        assert mock_call.call_count == 3  # bounded - no retry storm

    @pytest.mark.asyncio
    async def test_unclassified_error_defaults_to_permanent(self):
        provider = _fake_gemini_provider(AsyncMock(side_effect=RuntimeError("something unexpected")))
        with pytest.raises(LLMPermanentError):
            await provider.generate("prompt")


class TestGeminiApiKeyNeverLeaked:
    def test_h_key_not_in_provider_repr_or_dict(self):
        secret = "SUPER_SECRET_TEST_VALUE_NOT_REAL"
        provider = GeminiProvider(api_key=secret, model="gemini-2.5-flash")
        haystack = repr(provider) + str(provider.__dict__) + repr(provider.client)
        assert secret not in haystack

    @pytest.mark.asyncio
    async def test_h_key_not_in_exception_message_on_auth_failure(self):
        secret = "SUPER_SECRET_TEST_VALUE_NOT_REAL"
        auth_err = errors.ClientError(
            code=401, response_json={"error": {"message": "API key not valid", "status": "UNAUTHENTICATED", "code": 401}},
        )
        provider = GeminiProvider(api_key=secret, model="gemini-2.5-flash")
        provider.client.aio.models.generate_content = AsyncMock(side_effect=auth_err)

        with pytest.raises(LLMPermanentError) as exc_info:
            await provider.generate("prompt")
        assert secret not in str(exc_info.value)

    def test_h_missing_key_error_message_has_no_key_value(self):
        with pytest.raises(LLMPermanentError) as exc_info:
            GeminiProvider(api_key="", model="gemini-2.5-flash")
        # The error names the missing ENV VAR, never a key value (there is none to leak here).
        assert "GEMINI_API_KEY" in str(exc_info.value)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
