"""
Base agent class for all evaluation agents.
"""
import asyncio
import uuid
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional, TypeVar
from datetime import datetime
import json

from pydantic import ValidationError

from utils.logging import get_logger
from schemas.audit import AuditLog
from providers import get_llm_provider, LLMProvider
from providers.base import LLMPermanentError, LLMTransientError
from config.settings import MAX_RETRIES, TIMEOUT_SECONDS

T = TypeVar("T")


class StructuredOutputValidationError(Exception):
    """The LLM provider returned syntactically valid structured output, but
    it failed Pydantic validation against the agent's expected schema (a
    score out of range, a missing required field, an invalid enum value...).

    This is distinct from a provider-level failure (LLMTransientError /
    LLMPermanentError): the provider did its job (returned parseable JSON
    matching the requested shape); the CONTENT just doesn't satisfy the
    agent's stricter model. Treated as a bounded, retryable failure by
    BaseAgent._call_with_retry using the same attempt budget as provider
    failures - never a separate/unbounded loop - and surfaced as an explicit
    error if retries are exhausted. A schema validation failure must never
    silently become fake data (P1 STEP 12 policy)."""


class BaseAgent(ABC):
    """Base class for all agents."""

    def __init__(self, name: str, llm_provider: Optional[LLMProvider] = None):
        self.name = name
        self.llm_provider = llm_provider or get_llm_provider()
        self.logger = get_logger(f"agent.{name}")

    @abstractmethod
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Execute agent logic. Must be implemented by subclasses."""
        pass

    async def run(self, run_id: str, **kwargs) -> Dict[str, Any]:
        """Execute agent with audit logging."""
        start_time = datetime.now().isoformat()
        audit_log = AuditLog(
            log_id=f"log_{uuid.uuid4().hex[:8]}",
            run_id=run_id,
            agent_name=self.name,
            start_time=start_time,
            status="running",
        )

        try:
            self.logger.info(f"Starting execution with kwargs: {list(kwargs.keys())}")
            result = await self.execute(**kwargs)

            end_time = datetime.now().isoformat()
            duration = (datetime.fromisoformat(end_time) - datetime.fromisoformat(start_time)).total_seconds()

            audit_log.status = "success"
            audit_log.end_time = end_time
            audit_log.duration_seconds = duration
            audit_log.output_keys = list(result.keys()) if isinstance(result, dict) else []
            audit_log.input_keys = list(kwargs.keys())

            self.logger.info(f"Completed successfully in {duration:.2f}s")
            return {"result": result, "audit_log": audit_log}

        except Exception as e:
            self.logger.error(f"Execution failed: {str(e)}", exc_info=True)
            end_time = datetime.now().isoformat()
            duration = (datetime.fromisoformat(end_time) - datetime.fromisoformat(start_time)).total_seconds()

            audit_log.status = "failed"
            audit_log.end_time = end_time
            audit_log.duration_seconds = duration
            audit_log.error_message = str(e)

            return {"result": None, "audit_log": audit_log, "error": str(e)}

    async def call_llm_generate(
        self,
        prompt: str,
        *,
        max_retries: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        **kwargs,
    ) -> str:
        """Call LLM to generate text, with timeout + retry (P0-6)."""
        return await self._call_with_retry(
            self.llm_provider.generate, prompt, max_retries=max_retries, timeout_seconds=timeout_seconds, **kwargs
        )

    async def call_llm_structured(
        self,
        prompt: str,
        schema: Dict[str, Any],
        *,
        validate: Optional[Callable[[Dict[str, Any]], T]] = None,
        max_retries: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        **kwargs,
    ) -> T:
        """Call LLM to generate structured output, with timeout + retry (P0-6),
        optionally validated against a Pydantic model in the same call (P1).

        `schema` should normally be `SomeLLMOutputModel.model_json_schema()`
        (see schemas/llm_outputs.py) rather than a hand-written JSON Schema
        dict, so the requested shape and the validation model can never drift
        apart.

        If `validate` is given (typically `SomeLLMOutputModel.model_validate`),
        the provider's raw dict is validated on every attempt; a pydantic
        ValidationError is treated as a retryable malformed-output failure
        using the SAME bounded attempt budget as provider-level failures, and
        the validated model instance (not the raw dict) is returned. Without
        `validate`, behavior is unchanged: the raw dict is returned as-is.
        """
        async def _generate_and_validate(*a, **kw):
            raw = await self.llm_provider.generate_structured(*a, **kw)
            if validate is None:
                return raw
            try:
                return validate(raw)
            except ValidationError as e:
                raise StructuredOutputValidationError(
                    f"Structured output failed schema validation: {e}"
                ) from e

        return await self._call_with_retry(
            _generate_and_validate,
            prompt,
            schema,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
            **kwargs,
        )

    async def _call_with_retry(
        self,
        provider_fn,
        *args,
        max_retries: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        **kwargs,
    ):
        """Run one provider call with a timeout, retrying transient failures
        with exponential backoff.

        - A hung call is actually cancelled via asyncio.wait_for, not just
          abandoned.
        - LLMTransientError, timeouts, and StructuredOutputValidationError
          are retried; LLMPermanentError (bad API key, malformed request,
          etc.) fails immediately - retrying a configuration error can't fix
          it and just wastes calls.
        - Any other exception (a bug, an unclassified error) also fails
          immediately rather than being retried blindly.
        - Backoff is capped and bounded by max_retries, so a persistently
          failing provider fails clearly instead of retrying forever
          (no retry storm).
        - This all happens inside one execute() call, so BaseAgent.run()
          still writes exactly one audit log entry regardless of how many
          attempts were made.
        """
        retries = MAX_RETRIES if max_retries is None else max_retries
        timeout = TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds

        last_error: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                return await asyncio.wait_for(provider_fn(*args, **kwargs), timeout=timeout)
            except asyncio.TimeoutError as e:
                last_error = e
                self.logger.warning(
                    f"LLM call timed out after {timeout}s (attempt {attempt}/{retries})"
                )
            except LLMPermanentError as e:
                self.logger.error(f"LLM call failed permanently, not retrying: {e}")
                raise
            except LLMTransientError as e:
                last_error = e
                self.logger.warning(
                    f"Transient LLM error (attempt {attempt}/{retries}): {e}"
                )
            except StructuredOutputValidationError as e:
                last_error = e
                self.logger.warning(
                    f"Structured output failed schema validation (attempt {attempt}/{retries}): {e}"
                )

            if attempt < retries:
                backoff = min(0.5 * (2 ** (attempt - 1)), 5.0)
                await asyncio.sleep(backoff)

        raise RuntimeError(
            f"LLM call failed after {retries} attempt(s): {last_error}"
        ) from last_error

    def load_prompt(self, filename: str) -> str:
        """Load prompt template from file."""
        from config.settings import PROMPTS_DIR
        prompt_file = PROMPTS_DIR / filename
        if not prompt_file.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_file}")
        with open(prompt_file, 'r') as f:
            return f.read()
