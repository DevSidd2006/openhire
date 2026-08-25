"""
OpenAI LLM provider.
"""
import json
from typing import Any, Dict

from providers.base import LLMProvider, LLMTransientError, LLMPermanentError


class OpenAIProvider(LLMProvider):
    """OpenAI LLM provider."""

    def __init__(self, api_key: str, model: str = "gpt-4-turbo"):
        self.api_key = api_key
        self.model = model
        # Import here to avoid hard dependency
        try:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(api_key=api_key)
        except ImportError:
            raise ImportError("openai package required for OpenAIProvider. Install with: pip install openai")

    def _classify(self, e: Exception) -> Exception:
        """Map an openai SDK exception to LLMTransientError (safe to retry)
        or LLMPermanentError (retrying can never help). Unknown exceptions
        default to permanent - retrying a failure we don't understand is
        more dangerous than failing fast (avoids retry storms on bugs)."""
        try:
            import openai
        except ImportError:
            return LLMPermanentError(str(e))

        if isinstance(e, (
            openai.RateLimitError,
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.InternalServerError,
        )):
            return LLMTransientError(str(e))
        if isinstance(e, openai.APIStatusError) and (e.status_code in (408, 409, 429) or e.status_code >= 500):
            return LLMTransientError(str(e))
        return LLMPermanentError(str(e))

    async def generate(self, prompt: str, **kwargs) -> str:
        """Generate text from a prompt using OpenAI."""
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=kwargs.get("temperature", 0.7),
                max_tokens=kwargs.get("max_tokens", 2048),
            )
            return response.choices[0].message.content
        except Exception as e:
            raise self._classify(e) from e

    async def generate_structured(
        self, prompt: str, schema: Dict[str, Any], **kwargs
    ) -> Dict[str, Any]:
        """Generate structured output matching a schema.

        `schema` is expected to be a JSON Schema dict, normally produced by
        `SomeModel.model_json_schema()` (see schemas/llm_outputs.py) rather
        than hand-written - this method only needs it to be a JSON-Schema-
        shaped dict, it doesn't care where it came from.

        Uses OpenAI's Structured Outputs API (`response_format:
        {"type": "json_schema", ..., "strict": True}`), current as of the
        chat.completions API this provider targets - `strict: True` makes
        the API itself guarantee schema-conformant JSON, so failures here are
        almost always transport/response-shape problems (empty choices,
        empty/truncated content) rather than malformed JSON, but every case
        is still handled explicitly rather than assumed away.

        Error classification (see P1 STEP 12):
        - Network/rate-limit/timeout/5xx -> LLMTransientError (via
          _classify), retried by BaseAgent.
        - No choices / empty content / truncated (finish_reason == "length")
          / malformed JSON -> LLMTransientError: plausibly a one-off model
          hiccup, worth a bounded retry.
        - Auth/bad-request/model-not-found/etc. -> LLMPermanentError (via
          _classify), never retried.
        Never silently returns partial or fabricated data.
        """
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
                        "schema": schema,
                        "strict": True,
                    },
                },
            )
        except Exception as e:
            raise self._classify(e) from e

        if not response.choices:
            raise LLMTransientError("OpenAI structured response contained no choices")

        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        content = choice.message.content if choice.message else None

        if not content:
            raise LLMTransientError("OpenAI structured response had empty content")

        if finish_reason == "length":
            raise LLMTransientError(
                "OpenAI structured response was truncated before completion (finish_reason=length)"
            )

        try:
            return json.loads(content)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            raise LLMTransientError(f"OpenAI returned malformed structured JSON: {e}") from e
