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

    def __init__(self, api_key: str, model: str = "nvidia/nemotron-3-ultra-550b-a55b", base_url: str = "https://integrate.api.nvidia.com/v1"):
        """Initialize NVIDIA NIM provider.

        Args:
            api_key: NVIDIA API key (from https://build.nvidia.com)
            model: Model name (default: "nvidia/nemotron-3-ultra-550b-a55b" - ultra-powerful with extended thinking)
            base_url: Base URL for NIM endpoint (default: https://integrate.api.nvidia.com/v1)
        """
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

        # Import here to avoid hard dependency
        try:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
            )
        except ImportError:
            raise ImportError("openai package required for NvidiaNimProvider. Install with: pip install openai")

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
            )
            return response.choices[0].message.content
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
            )
            content = response.choices[0].message.content
            if not content:
                raise LLMTransientError("Empty response from NIM")
            try:
                return json.loads(content)
            except json.JSONDecodeError as e:
                raise LLMTransientError(f"Invalid JSON in response: {e}") from e

        except Exception as e:
            # If JSON mode isn't supported, try without it
            if "response_format" in str(e) or "json_object" in str(e):
                try:
                    response = await self.client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=kwargs.get("temperature", 0.1),
                        max_tokens=kwargs.get("max_tokens", 4096),
                    )
                    content = response.choices[0].message.content
                    if not content:
                        raise LLMTransientError("Empty response from NIM")
                    try:
                        return json.loads(content)
                    except json.JSONDecodeError as je:
                        raise LLMTransientError(f"Invalid JSON in response: {je}") from je
                except Exception as fallback_e:
                    raise self._classify(fallback_e) from fallback_e
            else:
                raise self._classify(e) from e
