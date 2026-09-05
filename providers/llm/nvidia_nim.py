"""
NVIDIA NIM (Inference Microservices) LLM provider.

NVIDIA NIM provides OpenAI-compatible API for running LLMs.
Supports both cloud-hosted and self-hosted NIM deployments.
"""
import json
from typing import Any, Dict

from providers.base import LLMProvider, LLMTransientError, LLMPermanentError


class NvidiaNimProvider(LLMProvider):
    """NVIDIA NIM LLM provider using OpenAI-compatible API."""

    def __init__(
        self,
        api_key: str,
        model: str = "nvidia/nemotron-3-super-120b-a12b",
        base_url: str = "https://integrate.api.nvidia.com/v1",
        enable_thinking: bool = False,
    ):
        """Initialize NVIDIA NIM provider.

        Args:
            api_key: NVIDIA API key (from https://build.nvidia.com)
            model: Model name. Default "nvidia/nemotron-3-super-120b-a12b" was
                the fastest AND widest option benchmarked (~1.1s median on a
                resume extraction, 360k-token prompts accepted).
                "nvidia/nemotron-3-ultra-550b-a55b" is the bigger model at ~13x
                the latency. Note "nemotron-3-nano-30b-a3b" is END OF LIFE and
                returns 410. See config/settings.py for the benchmark table.
            base_url: Base URL for NIM endpoint (default: https://integrate.api.nvidia.com/v1)
            enable_thinking: Whether to let Nemotron emit reasoning tokens before
                its answer. Defaults to False, and should stay off for everything
                this codebase does. Reasoning tokens are drawn from the SAME
                max_tokens budget as the answer, so with thinking on a short
                budget is spent entirely on reasoning and the call comes back
                finish_reason="length" with the answer never emitted - which is
                what the old "ultra-550b hangs past 45s" note in settings.py was
                actually observing. With thinking off, ultra-550b answers a
                trivial prompt in ~7-11s.
        """
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.enable_thinking = enable_thinking

        # Import here to avoid hard dependency
        try:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
            )
        except ImportError:
            raise ImportError("openai package required for NvidiaNimProvider. Install with: pip install openai")

    def _extra_body(self, **kwargs) -> Dict[str, Any]:
        """Nemotron toggles reasoning through the chat template, not a top-level
        field, so it has to travel in the OpenAI SDK's extra_body."""
        enable_thinking = kwargs.get("enable_thinking", self.enable_thinking)
        return {"chat_template_kwargs": {"enable_thinking": bool(enable_thinking)}}

    @staticmethod
    def _content(response) -> str:
        """Pull the answer out of a response.

        When a thinking-enabled call is truncated the endpoint does NOT leave
        content empty - it copies the partial reasoning into content as well, so
        a naive read hands the caller Nemotron's train of thought as if it were
        the answer. Detect that (finish_reason "length" with content that is
        just the reasoning) and raise instead, so BaseAgent retries rather than
        scoring a candidate against a half-finished thought.
        """
        choice = response.choices[0]
        content = choice.message.content or ""
        reasoning = getattr(choice.message, "reasoning_content", None) or ""
        truncated = getattr(choice, "finish_reason", None) == "length"

        if truncated and (not content or content.strip() == reasoning.strip()):
            raise LLMTransientError(
                "NIM response hit max_tokens before emitting an answer "
                "(reasoning tokens consumed the budget); raise max_tokens or "
                "disable thinking"
            )
        if not content:
            raise LLMTransientError("Empty response from NIM")
        return content

    def _classify(self, e: Exception) -> Exception:
        """Map OpenAI SDK exceptions to transient/permanent errors."""
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
        """Generate text from a prompt using NVIDIA NIM."""
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=kwargs.get("temperature", 0.7),
                max_tokens=kwargs.get("max_tokens", 2048),
                extra_body=self._extra_body(**kwargs),
            )
            return self._content(response)
        except LLMTransientError:
            raise
        except Exception as e:
            raise self._classify(e) from e

    async def generate_structured(
        self, prompt: str, schema: Dict[str, Any], **kwargs
    ) -> Dict[str, Any]:
        """Generate structured output matching a JSON schema using NVIDIA NIM.

        Note: Not all NIM models support JSON mode. If unsupported, falls back
        to parsing the response as JSON.
        """
        try:
            # Try with JSON mode if available
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=kwargs.get("temperature", 0.1),
                max_tokens=kwargs.get("max_tokens", 4096),
                response_format={"type": "json_object"},
                extra_body=self._extra_body(**kwargs),
            )
            content = self._content(response)
            try:
                return json.loads(content)
            except json.JSONDecodeError as e:
                raise LLMTransientError(f"Invalid JSON in response: {e}") from e

        except LLMTransientError:
            raise
        except Exception as e:
            # If JSON mode isn't supported, try without it
            if "response_format" in str(e) or "json_object" in str(e):
                try:
                    response = await self.client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=kwargs.get("temperature", 0.1),
                        max_tokens=kwargs.get("max_tokens", 4096),
                        extra_body=self._extra_body(**kwargs),
                    )
                    content = self._content(response)
                    try:
                        return json.loads(content)
                    except json.JSONDecodeError as je:
                        raise LLMTransientError(f"Invalid JSON in response: {je}") from je
                except Exception as fallback_e:
                    raise self._classify(fallback_e) from fallback_e
            else:
                raise self._classify(e) from e
