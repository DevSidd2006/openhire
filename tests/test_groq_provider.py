"""
P8B.3: Groq provider tests - OpenHire's PRIMARY real provider.

Every SDK call here is mocked; this file NEVER makes a real network call
and never uses a real API key. These are LOCAL REGRESSION tests: they pin
down the deterministic behavior of GroqProvider (schema adaptation, error
classification, response-shape handling) so it cannot silently regress.

Real behavioral evaluation of openai/gpt-oss-20b is a separate activity -
see `python -m evaluation.runner --provider groq` and the P8B.3 report.

The Groq strict-mode rules asserted below were established empirically
against the live API (see providers/llm/groq.py's module docstring):
strict mode requires additionalProperties:false on every object AND a
complete `required` list, and cannot express an open-ended map at all.
"""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

import groq

from providers.base import LLMPermanentError, LLMTransientError
from providers.llm.groq import GroqProvider
from schemas.llm_outputs import (
    JDAnalysisResult,
    ResumeParseResult,
    TechnicalEvaluationResult,
)

FAKE_KEY = "test-key-not-real"


def _provider(create_mock) -> GroqProvider:
    """A real GroqProvider (construction makes no network call) with its
    single SDK entry point replaced by a mock."""
    provider = GroqProvider(api_key=FAKE_KEY, model="openai/gpt-oss-20b")
    provider.client.chat.completions.create = create_mock
    return provider


def _response(content, finish_reason="stop", choices=True):
    choice = MagicMock()
    choice.finish_reason = finish_reason
    choice.message = MagicMock()
    choice.message.content = content
    resp = MagicMock()
    resp.choices = [choice] if choices else []
    return resp


def _status_error(status_code, code=None):
    """Build a groq.APIStatusError carrying a structured error body, the
    way the real SDK does."""
    body = {"error": {"message": "boom", "code": code}} if code else {"error": {"message": "boom"}}
    response = MagicMock()
    response.status_code = status_code
    err = groq.APIStatusError("boom", response=response, body=body)
    return err


class TestConstruction:
    def test_constructs_with_key_and_model(self):
        provider = GroqProvider(api_key=FAKE_KEY, model="openai/gpt-oss-20b")
        assert provider.model == "openai/gpt-oss-20b"

    def test_default_model_is_the_benchmark_model(self):
        assert GroqProvider(api_key=FAKE_KEY).model == "openai/gpt-oss-20b"

    def test_missing_key_fails_at_construction_not_first_call(self):
        with pytest.raises(LLMPermanentError):
            GroqProvider(api_key="")

    def test_missing_key_error_never_leaks_a_key(self):
        try:
            GroqProvider(api_key="")
        except LLMPermanentError as exc:
            assert FAKE_KEY not in str(exc)


class TestSchemaAdaptationForStrictMode:
    """prepare_schema() is the whole Groq-specific adaptation, and it is
    pure - so it is tested directly, with no network involved."""

    def test_flat_schema_becomes_strict(self):
        schema = {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        }
        sent, strict = GroqProvider.prepare_schema(schema)
        assert strict is True
        assert sent["additionalProperties"] is False

    def test_every_nested_object_gets_additional_properties_false(self):
        """Groq rejects a schema whose $defs objects lack the flag - the
        exact 400 that raw Model.model_json_schema() output produces."""
        sent, strict = GroqProvider.prepare_schema(ResumeParseResult.model_json_schema())
        assert strict is True

        def every_object_closed(node):
            if isinstance(node, dict):
                if isinstance(node.get("properties"), dict):
                    if node.get("additionalProperties") is not False:
                        return False
                return all(every_object_closed(v) for v in node.values())
            if isinstance(node, list):
                return all(every_object_closed(v) for v in node)
            return True

        assert every_object_closed(sent)

    def test_required_lists_every_property(self):
        """Pydantic omits defaulted fields from `required`; Groq strict mode
        demands all of them."""
        sent, _ = GroqProvider.prepare_schema(JDAnalysisResult.model_json_schema())
        assert set(sent["required"]) == set(sent["properties"].keys())

    def test_open_ended_map_falls_back_to_non_strict_untouched(self):
        """Dict[str, CompetencyJudgment] cannot be expressed in strict mode.
        The provider must NOT weaken the agent's schema - it sends the
        original and turns strict off for that request only."""
        original = TechnicalEvaluationResult.model_json_schema()
        sent, strict = GroqProvider.prepare_schema(original)
        assert strict is False
        assert sent == original

    def test_caller_schema_is_never_mutated(self):
        original = JDAnalysisResult.model_json_schema()
        before = json.dumps(original, sort_keys=True)
        GroqProvider.prepare_schema(original)
        assert json.dumps(original, sort_keys=True) == before


class TestErrorClassification:
    """Retrying is BaseAgent's job; classifying is the provider's. A
    misclassification either burns the retry budget on a hopeless call or
    fails fast on a recoverable one."""

    @pytest.mark.asyncio
    async def test_json_validate_failed_400_is_transient(self):
        """Groq returns HTTP 400 when the MODEL fails to produce
        schema-valid JSON. Unlike every other 400 this is a per-sample
        hiccup and the identical call can succeed on retry, so it must be
        transient - verified against the live API."""
        create = AsyncMock(side_effect=_status_error(400, code="json_validate_failed"))
        with pytest.raises(LLMTransientError):
            await _provider(create).generate_structured("p", {"type": "object", "properties": {}})

    @pytest.mark.asyncio
    async def test_ordinary_400_is_permanent(self):
        """A genuinely invalid request/schema can never be fixed by
        retrying."""
        create = AsyncMock(side_effect=_status_error(400))
        with pytest.raises(LLMPermanentError):
            await _provider(create).generate_structured("p", {"type": "object", "properties": {}})

    @pytest.mark.asyncio
    async def test_rate_limit_429_is_transient(self):
        create = AsyncMock(side_effect=_status_error(429))
        with pytest.raises(LLMTransientError):
            await _provider(create).generate("p")

    @pytest.mark.asyncio
    async def test_server_error_is_transient(self):
        create = AsyncMock(side_effect=_status_error(503))
        with pytest.raises(LLMTransientError):
            await _provider(create).generate("p")

    @pytest.mark.asyncio
    async def test_auth_error_is_permanent(self):
        create = AsyncMock(side_effect=_status_error(401))
        with pytest.raises(LLMPermanentError):
            await _provider(create).generate("p")

    @pytest.mark.asyncio
    async def test_unknown_exception_is_permanent_not_retried(self):
        create = AsyncMock(side_effect=ValueError("a bug, not a network blip"))
        with pytest.raises(LLMPermanentError):
            await _provider(create).generate("p")

    def test_json_validate_failed_message_never_leaks_generated_text(self):
        """`failed_generation` can contain the candidate's own text; it must
        not be copied into an exception that ends up in logs/reports."""
        response = MagicMock()
        response.status_code = 400
        err = groq.APIStatusError(
            "boom",
            response=response,
            body={"error": {"code": "json_validate_failed",
                            "failed_generation": "SECRET-CANDIDATE-TEXT"}},
        )
        classified = GroqProvider(api_key=FAKE_KEY)._classify(err)
        assert isinstance(classified, LLMTransientError)
        assert "SECRET-CANDIDATE-TEXT" not in str(classified)


class TestResponseHandling:
    @pytest.mark.asyncio
    async def test_valid_structured_response_is_parsed(self):
        create = AsyncMock(return_value=_response('{"title": "Engineer"}'))
        out = await _provider(create).generate_structured(
            "p", {"type": "object", "properties": {"title": {"type": "string"}}}
        )
        assert out == {"title": "Engineer"}

    @pytest.mark.asyncio
    async def test_empty_content_is_transient(self):
        create = AsyncMock(return_value=_response(""))
        with pytest.raises(LLMTransientError):
            await _provider(create).generate_structured("p", {"type": "object", "properties": {}})

    @pytest.mark.asyncio
    async def test_no_choices_is_transient(self):
        create = AsyncMock(return_value=_response(None, choices=False))
        with pytest.raises(LLMTransientError):
            await _provider(create).generate_structured("p", {"type": "object", "properties": {}})

    @pytest.mark.asyncio
    async def test_truncated_response_is_transient_not_partial_data(self):
        """A truncated response must never be handed on as if it were
        complete."""
        create = AsyncMock(return_value=_response('{"title": "Eng"', finish_reason="length"))
        with pytest.raises(LLMTransientError):
            await _provider(create).generate_structured("p", {"type": "object", "properties": {}})

    @pytest.mark.asyncio
    async def test_malformed_json_is_transient_never_fabricated(self):
        create = AsyncMock(return_value=_response("I'm sorry, I can't do that."))
        with pytest.raises(LLMTransientError):
            await _provider(create).generate_structured("p", {"type": "object", "properties": {}})

    @pytest.mark.asyncio
    async def test_request_carries_strict_json_schema_response_format(self):
        create = AsyncMock(return_value=_response('{"city": "Paris"}'))
        await _provider(create).generate_structured(
            "p", {"type": "object", "properties": {"city": {"type": "string"}}}
        )
        rf = create.call_args.kwargs["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["strict"] is True
        assert rf["json_schema"]["schema"]["additionalProperties"] is False

    @pytest.mark.asyncio
    async def test_map_bearing_request_sends_strict_false(self):
        create = AsyncMock(return_value=_response('{"technical_score": 5.0}'))
        await _provider(create).generate_structured(
            "p", TechnicalEvaluationResult.model_json_schema()
        )
        assert create.call_args.kwargs["response_format"]["json_schema"]["strict"] is False

    @pytest.mark.asyncio
    async def test_plain_generate_returns_text(self):
        create = AsyncMock(return_value=_response("hello"))
        assert await _provider(create).generate("p") == "hello"
