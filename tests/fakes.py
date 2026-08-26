"""Test doubles for LLMProvider. Never make real network/API calls."""
import asyncio
import json
from typing import Any, Dict, List, Optional

from providers.base import LLMProvider, LLMTransientError


class ScriptedLLMProvider(LLMProvider):
    """A fake LLMProvider that plays back a fixed script of responses.

    Each entry in `script` is consumed in order on successive calls to
    generate(); once exhausted, the last entry repeats. An entry that is an
    Exception instance is raised instead of returned. If `hang_seconds` is
    set, every call sleeps that long before consulting the script - used to
    simulate a provider call that never returns in time, so retry/timeout
    logic can be exercised without a real network hang.
    """

    def __init__(self, script: Optional[List[Any]] = None, hang_seconds: Optional[float] = None):
        self.script = list(script) if script else ["Mock response to prompt"]
        self.hang_seconds = hang_seconds
        self.calls: List[str] = []
        self._index = 0

    def _next(self) -> Any:
        if self._index < len(self.script):
            item = self.script[self._index]
            self._index += 1
        else:
            item = self.script[-1]
        return item

    async def generate(self, prompt: str, **kwargs) -> str:
        self.calls.append(prompt)
        if self.hang_seconds is not None:
            await asyncio.sleep(self.hang_seconds)
        item = self._next()
        if isinstance(item, Exception):
            raise item
        return item

    async def generate_structured(self, prompt: str, schema: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        text = await self.generate(prompt, **kwargs)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            # Matches providers/llm/openai.py's real classification of
            # malformed JSON text as a retryable transient error (not an
            # immediate hard failure) - without this, a script entry that
            # is deliberately invalid JSON would bypass BaseAgent's bounded
            # retry entirely instead of exercising it, giving a false
            # picture of retry/fallback behavior in tests that use this
            # fake to simulate malformed structured output.
            raise LLMTransientError(f"Malformed JSON from scripted response: {e}") from e


class SpyLLMProvider(LLMProvider):
    """Wraps a real LLMProvider and records which method each call actually
    used, so a test can assert an agent calls generate_structured() (not the
    unstructured generate() + manual json.loads() it used before P1) without
    having to fake the whole response - the wrapped provider (typically a
    real MockLLMProvider) still does the actual work."""

    def __init__(self, delegate: LLMProvider):
        self.delegate = delegate
        self.generate_calls: List[str] = []
        self.generate_structured_calls: List[str] = []

    async def generate(self, prompt: str, **kwargs) -> str:
        self.generate_calls.append(prompt)
        return await self.delegate.generate(prompt, **kwargs)

    async def generate_structured(self, prompt: str, schema: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        self.generate_structured_calls.append(prompt)
        return await self.delegate.generate_structured(prompt, schema, **kwargs)


class SingleResponseLLMProvider(LLMProvider):
    """A fake LLMProvider that returns one fixed JSON string for every call
    and records every prompt it was given, so a test can assert on what
    reached the prompt (e.g. that bias-checker rationale text is present)."""

    def __init__(self, response_json: str):
        self.response_json = response_json
        self.calls: List[str] = []

    async def generate(self, prompt: str, **kwargs) -> str:
        self.calls.append(prompt)
        return self.response_json

    async def generate_structured(self, prompt: str, schema: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        text = await self.generate(prompt, **kwargs)
        return json.loads(text)
