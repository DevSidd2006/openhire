"""
Gemini LLM provider (P8B).

Mirrors providers/llm/openai.py's structure and error-classification
philosophy exactly - same LLMProvider interface, same
LLMTransientError/LLMPermanentError boundary BaseAgent's retry wrapper
already understands, same "never fabricate on failure" policy. No second
retry mechanism is introduced here; classification is this provider's only
job, exactly like OpenAIProvider.
"""
import json
from typing import Any, Dict

from providers.base import LLMProvider, LLMPermanentError, LLMTransientError

# Finish reasons that mean "the model declined/was blocked from producing a
# real answer to this exact prompt" - retrying the SAME prompt will not
# change that, so these are permanent, not transient.
_BLOCKED_FINISH_REASONS = frozenset({
    "SAFETY", "RECITATION", "LANGUAGE", "BLOCKLIST",
    "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT",
})


class GeminiProvider(LLMProvider):
    """Gemini LLM provider (Google `google-genai` SDK)."""

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        if not api_key:
            # Fail at construction, not on first use - matches the factory's
            # existing OPENAI_API_KEY check (providers/llm/__init__.py)
            # rather than deferring the failure into a confusing first call.
            raise LLMPermanentError(
                "GEMINI_API_KEY is required for the Gemini provider"
            )
        self.model = model
        # Import here to avoid a hard dependency when LLM_PROVIDER != "gemini"
        # (same pattern as OpenAIProvider).
        try:
            from google import genai
            self.client = genai.Client(api_key=api_key)
        except ImportError:
            raise ImportError(
                "google-genai package required for GeminiProvider. Install with: pip install google-genai"
            )

    def _classify(self, e: Exception) -> Exception:
        """Map a google-genai SDK exception to LLMTransientError (safe to
        retry) or LLMPermanentError (retrying can never help). Unknown
        exceptions default to permanent - same fail-fast rationale as
        OpenAIProvider._classify: retrying a failure we don't understand is
        more dangerous than assuming it's safe to retry."""
        try:
            from google.genai import errors
        except ImportError:
            return LLMPermanentError(str(e))

        if isinstance(e, errors.APIError):
            code = getattr(e, "code", None)
            if code == 429 or (isinstance(code, int) and code >= 500):
                return LLMTransientError(str(e))
            return LLMPermanentError(str(e))
        return LLMPermanentError(str(e))

    def _config(self, **kwargs):
        from google.genai import types
        return types.GenerateContentConfig(
            temperature=kwargs.get("temperature", 0.7),
            max_output_tokens=kwargs.get("max_tokens", 2048),
            # This provider never passes `tools`, so there is nothing for
            # Automatic Function Calling to do - only the SDK's own
            # per-call discovery/dispatch overhead it warns about
            # ("Direct use of AFC in AsyncModels.generate_content is not
            # recommended"). Disabling it removes that overhead entirely.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    async def generate(self, prompt: str, **kwargs) -> str:
        """Generate text from a prompt using Gemini."""
        try:
            response = await self.client.aio.models.generate_content(
                model=self.model, contents=prompt, config=self._config(**kwargs),
            )
        except Exception as e:
            raise self._classify(e) from e

        finish_reason = self._finish_reason(response)
        if finish_reason in _BLOCKED_FINISH_REASONS:
            raise LLMPermanentError(f"Gemini declined to respond (finish_reason={finish_reason})")

        text = response.text
        if not text:
            raise LLMTransientError("Gemini response had empty content")
        if finish_reason == "MAX_TOKENS":
            raise LLMTransientError("Gemini response was truncated (finish_reason=MAX_TOKENS)")
        return text

    async def generate_structured(
        self, prompt: str, schema: Dict[str, Any], **kwargs
    ) -> Dict[str, Any]:
        """Generate structured output matching a schema.

        `schema` is expected to be a JSON Schema dict, normally produced by
        `SomeModel.model_json_schema()` - passed straight through to
        Gemini's `response_json_schema` config field, which accepts JSON
        Schema directly (a documented alternative to Gemini's own
        proprietary `response_schema` type) - no hand conversion needed,
        and no weakening of the schema to fit a Gemini-specific format.
        `response_mime_type="application/json"` is required alongside it.

        Error classification mirrors providers/llm/openai.py exactly:
        network/rate-limit/5xx -> LLMTransientError (retried); a safety/
        content block or auth/bad-request -> LLMPermanentError (never
        retried); empty/truncated/malformed JSON -> LLMTransientError (a
        plausible one-off, worth a bounded retry). Never returns partial or
        fabricated data.
        """
        from google.genai import types
        # Structured output (a full nested schema like ResumeParseResult)
        # needs far more headroom than generate()'s 2048-token default -
        # verified locally: a real resume prompt against ResumeParseResult
        # hit finish_reason=MAX_TOKENS at 2048 and completed correctly at
        # 8192.
        kwargs.setdefault("max_tokens", 8192)
        config = self._config(**kwargs)
        config.response_mime_type = "application/json"
        config.response_json_schema = schema

        try:
            response = await self.client.aio.models.generate_content(
                model=self.model, contents=prompt, config=config,
            )
        except Exception as e:
            raise self._classify(e) from e

        finish_reason = self._finish_reason(response)
        if finish_reason in _BLOCKED_FINISH_REASONS:
            raise LLMPermanentError(f"Gemini declined to respond (finish_reason={finish_reason})")

        text = response.text
        if not text:
            raise LLMTransientError("Gemini structured response had empty content")
        if finish_reason == "MAX_TOKENS":
            raise LLMTransientError("Gemini structured response was truncated (finish_reason=MAX_TOKENS)")

        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            raise LLMTransientError(f"Gemini returned malformed structured JSON: {e}") from e

    @staticmethod
    def _finish_reason(response) -> Any:
        candidates = getattr(response, "candidates", None)
        if not candidates:
            return None
        reason = getattr(candidates[0], "finish_reason", None)
        # google-genai's FinishReason is a str Enum - .value gives the plain
        # string ("STOP", "MAX_TOKENS", ...) so callers can compare against
        # plain strings without importing the SDK's enum type.
        return getattr(reason, "value", reason)
