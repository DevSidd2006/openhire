"""
Groq LLM provider (P8B.3) - OpenHire's PRIMARY real LLM provider.

Mirrors providers/llm/openai.py and providers/llm/gemini.py exactly - same
LLMProvider interface, same LLMTransientError/LLMPermanentError boundary
BaseAgent's retry wrapper already understands, same "never fabricate on
failure" policy. No second retry mechanism is introduced here; error
classification and Groq-specific schema adaptation are this provider's only
jobs.

Structured output on Groq (empirically verified against openai/gpt-oss-20b,
see the P8B.3 report). Groq exposes OpenAI-style
response_format={"type": "json_schema", ...} with a "strict" flag, and the
two modes behave very differently:

  * strict=True - the API validates the schema up front and constrains
    decoding. It ENFORCES numeric bounds (a "score" declared 0.0 <= x <= 1.0
    cannot come back as 4.5) and enum membership. But it imposes two
    structural requirements that a raw Model.model_json_schema() never
    satisfies:
      1. "additionalProperties": false on EVERY object, including objects
         nested inside the schema's definitions section.
      2. "required" must list EVERY key in "properties" (Pydantic omits
         fields that have defaults).
  * strict=False - the schema is accepted as-is, including definitions,
    references and open-ended maps, and the model still returns
    shape-conformant JSON, but numeric bounds are NOT enforced (verified: it
    happily returned score 4.5 for a 0-1 field).

So this provider prefers strict and adapts the schema to meet Groq's two
structural requirements (_normalize_for_strict). Adding
"additionalProperties": false and completing "required" does not weaken the
contract - it tightens it, and Pydantic re-validates everything afterwards
in BaseAgent.call_llm_structured either way.
"""
# The one schema shape Groq's strict mode genuinely cannot express is an
# OPEN-ENDED MAP - e.g. Dict[str, CompetencyJudgment] in
# TechnicalEvaluationResult / BehavioralEvaluationResult, whose competency
# keys are not known ahead of time. Groq demands "additionalProperties":
# false there, which would forbid every key and make the field useless.
# Rather than weaken those agent schemas (P8B.3 Phase 3 explicitly forbids
# that), _needs_open_ended_map detects the shape and this provider falls
# back to strict=False FOR THAT REQUEST ONLY, sending the original schema
# untouched. Numeric bounds are then enforced one layer up instead, by
# Pydantic validation + BaseAgent's bounded retry. The adaptation lives
# here, in the provider, so no agent has to know anything about Groq.
#
# This provider never logs, prints, or embeds the API key - not in
# messages, not in exceptions.
import copy
import json
from typing import Any, Dict, Tuple

from providers.base import LLMProvider, LLMPermanentError, LLMTransientError

# Groq surfaces "the model failed to produce JSON satisfying the schema" as
# an HTTP 400 with this code (also used when the response is cut off by
# max_tokens mid-object, in which case failed_generation is empty). A 400
# would normally be permanent, but this particular one is a per-sample model
# hiccup, not a broken request - the very same call can and does succeed on
# a retry, so it is classified transient. Verified against gpt-oss-20b.
_JSON_VALIDATE_FAILED = "json_validate_failed"


class GroqProvider(LLMProvider):
    """Groq LLM provider (official groq SDK, AsyncGroq client)."""

    def __init__(self, api_key: str, model: str = "openai/gpt-oss-20b"):
        if not api_key:
            # Fail at construction, not on first use - matches GeminiProvider
            # and the factory's existing API-key checks.
            raise LLMPermanentError("GROQ_API_KEY is required for the Groq provider")
        self.model = model
        # Import here to avoid a hard dependency when LLM_PROVIDER != "groq"
        # (same pattern as OpenAIProvider/GeminiProvider).
        try:
            from groq import AsyncGroq
            self.client = AsyncGroq(api_key=api_key)
        except ImportError:
            raise ImportError(
                "groq package required for GroqProvider. Install with: pip install groq"
            )

    # -- error classification ------------------------------------------------

    def _classify(self, e: Exception) -> Exception:
        """Map a groq SDK exception to LLMTransientError (safe to retry) or
        LLMPermanentError (retrying can never help). Unknown exceptions
        default to permanent - retrying a failure we do not understand is
        more dangerous than failing fast (avoids retry storms on bugs).
        Same rationale as OpenAIProvider._classify."""
        try:
            import groq
        except ImportError:
            return LLMPermanentError(str(e))

        if isinstance(e, (
            groq.RateLimitError,
            groq.APITimeoutError,
            groq.APIConnectionError,
            groq.InternalServerError,
        )):
            return LLMTransientError(str(e))

        if isinstance(e, groq.APIStatusError):
            status = getattr(e, "status_code", None)
            # The model-produced-bad-JSON 400 is retryable (see
            # _JSON_VALIDATE_FAILED above); every other 400 is a real
            # request/schema/auth problem and must fail fast.
            if status == 400 and self._is_json_validate_failed(e):
                return LLMTransientError(
                    "Groq could not produce schema-valid JSON for this prompt "
                    "(code=" + _JSON_VALIDATE_FAILED + ")"
                )
            if isinstance(status, int) and (status in (408, 409, 429) or status >= 500):
                return LLMTransientError(str(e))
            return LLMPermanentError(str(e))

        return LLMPermanentError(str(e))

    @staticmethod
    def _is_json_validate_failed(e: Exception) -> bool:
        """True if this APIStatusError is Groq's json_validate_failed.

        Reads the structured error body when the SDK exposes one and falls
        back to a substring check on the message, so a change in SDK shape
        degrades to "still detected" rather than "silently misclassified as
        permanent". Deliberately does NOT surface failed_generation (the
        model's raw partial text) into the exception message - that keeps
        prompt and response content out of logs.
        """
        body = getattr(e, "body", None)
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("code") == _JSON_VALIDATE_FAILED:
                return True
        return _JSON_VALIDATE_FAILED in str(e)

    # -- Groq strict-mode schema adaptation ----------------------------------

    @staticmethod
    def _needs_open_ended_map(node: Any) -> bool:
        """True if node (anywhere in the schema tree) contains an object
        whose keys are not known ahead of time - i.e. a Dict[str, X], which
        Pydantic renders as an object with "additionalProperties" pointing
        at a sub-schema (or an object with no "properties" at all). Groq's
        strict mode cannot express that shape, so its presence forces the
        non-strict path for the whole request.
        """
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" not in node:
                # Either a typed map (additionalProperties is a sub-schema)
                # or a bare free-form dict - neither can be pinned down by
                # strict mode.
                return True
            if isinstance(node.get("additionalProperties"), dict):
                return True
            return any(GroqProvider._needs_open_ended_map(v) for v in node.values())
        if isinstance(node, list):
            return any(GroqProvider._needs_open_ended_map(v) for v in node)
        return False

    @staticmethod
    def _normalize_for_strict(node: Any) -> Any:
        """Make a JSON Schema satisfy Groq's two strict-mode structural
        rules, in place, on an already-copied tree:

          1. every object gets "additionalProperties": false
          2. every object's "required" lists all of its "properties"

        Rule 2 looks like it changes the contract, but it does not weaken
        it: Pydantic fields with defaults are omitted from "required" only
        because they MAY be absent, and every such field in
        schemas/llm_outputs.py is either nullable or has a default the model
        can legitimately supply. Requiring the model to emit them removes
        the ambiguity of "field missing vs. field deliberately empty", and
        Pydantic still validates the result afterwards.

        Recurses through definitions, properties, items and
        anyOf/oneOf/allOf alike by simply walking every value.
        """
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                node["additionalProperties"] = False
                node["required"] = list(props.keys())
            for value in node.values():
                GroqProvider._normalize_for_strict(value)
        elif isinstance(node, list):
            for value in node:
                GroqProvider._normalize_for_strict(value)
        return node

    @classmethod
    def prepare_schema(cls, schema: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """Return (schema_to_send, strict) for one structured request.

        Pure and side-effect free - the caller's schema dict is never
        mutated, so passing SomeModel.model_json_schema() straight through
        stays safe. Exposed as a classmethod so the adaptation is directly
        unit-testable without any network call.
        """
        if cls._needs_open_ended_map(schema):
            return schema, False
        return cls._normalize_for_strict(copy.deepcopy(schema)), True

    # -- LLMProvider interface -----------------------------------------------

    async def generate(self, prompt: str, **kwargs) -> str:
        """Generate text from a prompt using Groq."""
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=kwargs.get("temperature", 0.7),
                max_tokens=kwargs.get("max_tokens", 2048),
            )
        except Exception as e:
            raise self._classify(e) from e

        if not response.choices:
            raise LLMTransientError("Groq response contained no choices")

        choice = response.choices[0]
        content = choice.message.content if choice.message else None
        if not content:
            raise LLMTransientError("Groq response had empty content")
        if getattr(choice, "finish_reason", None) == "length":
            raise LLMTransientError(
                "Groq response was truncated before completion (finish_reason=length)"
            )
        return content

    async def generate_structured(
        self, prompt: str, schema: Dict[str, Any], **kwargs
    ) -> Dict[str, Any]:
        """Generate structured output matching a JSON Schema.

        schema is expected to be a JSON Schema dict, normally produced by
        SomeModel.model_json_schema() (see schemas/llm_outputs.py). It is
        adapted for Groq by prepare_schema (see this module's docstring) and
        is never mutated.

        Error classification mirrors providers/llm/openai.py:
        - Network/rate-limit/timeout/5xx -> LLMTransientError (retried by
          BaseAgent).
        - Groq's json_validate_failed 400, no choices, empty content,
          truncation, malformed JSON -> LLMTransientError: a plausible
          one-off model hiccup, worth a bounded retry.
        - Auth, genuinely invalid schema, unknown model -> permanent, never
          retried.
        Never silently returns partial or fabricated data.
        """
        send_schema, strict = self.prepare_schema(schema)
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=kwargs.get("temperature", 0.7),
                max_tokens=kwargs.get("max_tokens", 2048),
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "response",
                        "schema": send_schema,
                        "strict": strict,
                    },
                },
            )
        except Exception as e:
            raise self._classify(e) from e

        if not response.choices:
            raise LLMTransientError("Groq structured response contained no choices")

        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        content = choice.message.content if choice.message else None

        if not content:
            raise LLMTransientError("Groq structured response had empty content")
        if finish_reason == "length":
            raise LLMTransientError(
                "Groq structured response was truncated before completion "
                "(finish_reason=length)"
            )

        try:
            return json.loads(content)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            raise LLMTransientError(
                "Groq returned malformed structured JSON: " + str(e)
            ) from e
