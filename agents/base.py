"""
Base agent class for all evaluation agents.
"""
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from datetime import datetime
import json

from utils.logging import get_logger
from schemas.audit import AuditLog
from providers import get_llm_provider, LLMProvider


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

    async def call_llm_generate(self, prompt: str, **kwargs) -> str:
        """Call LLM to generate text."""
        return await self.llm_provider.generate(prompt, **kwargs)

    async def call_llm_structured(self, prompt: str, schema: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """Call LLM to generate structured output."""
        return await self.llm_provider.generate_structured(prompt, schema, **kwargs)

    def load_prompt(self, filename: str) -> str:
        """Load prompt template from file."""
        from config.settings import PROMPTS_DIR
        prompt_file = PROMPTS_DIR / filename
        if not prompt_file.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_file}")
        with open(prompt_file, 'r') as f:
            return f.read()
