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

class GeminiProvider(LLMProvider):
    """Gemini LLM provider (Google `google-genai` SDK)."""

    def __init__(self, api_key: str, model: str = "gemini-3.5-flash-lite"):
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

    @staticmethod
    def _generation_config(**kwargs) -> dict[str, Any]:
        config: dict[str, Any] = {"max_output_tokens": kwargs.get("max_tokens", 2048)}
        if "temperature" in kwargs:
            config["temperature"] = kwargs["temperature"]
        return config

    async def generate(self, prompt: str, **kwargs) -> str:
        """Generate text from a prompt using Gemini."""
        try:
            response = await self.client.aio.interactions.create(
                model=self.model,
                input=prompt,
                generation_config=self._generation_config(**kwargs),
            )
        except Exception as e:
            raise self._classify(e) from e

        self._require_completed(response)
        text = response.output_text
        if not text:
            raise LLMTransientError("Gemini response had empty content")
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

        Error classification mirrors providers/llm/openai.py: network,
        rate-limit, incomplete, and 5xx failures are transient; auth,
        bad-request, cancelled, and failed interactions are permanent.
        Empty or malformed JSON is transient. Partial data is never returned.
        """
        # Structured output (a full nested schema like ResumeParseResult)
        # needs far more headroom than generate()'s 2048-token default -
        # verified locally: a real resume prompt against ResumeParseResult
        # hit finish_reason=MAX_TOKENS at 2048 and completed correctly at
        # 8192.
        kwargs.setdefault("max_tokens", 8192)
        try:
            response = await self.client.aio.interactions.create(
                model=self.model,
                input=prompt,
                generation_config=self._generation_config(**kwargs),
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": schema,
                },
            )
        except Exception as e:
            raise self._classify(e) from e

        self._require_completed(response)
        text = response.output_text
        if not text:
            raise LLMTransientError("Gemini structured response had empty content")

        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            raise LLMTransientError(f"Gemini returned malformed structured JSON: {e}") from e

    @staticmethod
    def _require_completed(response) -> None:
        status = getattr(response, "status", None)
        if status == "completed":
            return
        errors = getattr(response, "errors", None) or []
        detail = "; ".join(str(error) for error in errors) or str(status or "unknown")
        if status in {"incomplete", "budget_exceeded", "in_progress", "queued"}:
            raise LLMTransientError(f"Gemini interaction did not complete ({detail})")
        raise LLMPermanentError(f"Gemini interaction failed ({detail})")
