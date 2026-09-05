"""
P6 Phase 2/3: evaluation case and result models.

Deliberately NOT a duplicate of any domain schema (schemas/*.py) - these are
evaluation-framework-only DTOs. `EvaluationCase.input` and `checks` are
plain dicts/lists rather than a rigid typed schema because different agents
need genuinely different input shapes (a JD analyzer case needs raw text; a
technical evaluator case needs a job/resume/transcript/simulated-LLM-script)
and different check parameters - a fixed schema here would just become an
untyped dict by another name for half of them anyway.
"""
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class EvaluationCase(BaseModel):
    """One golden-dataset case (P6 Phase 2). Loaded from
    evaluation/cases/<agent>.json - see evaluation/cases/README.md (in the
    module docstring of runner.py) for the file format."""
    case_id: str
    agent: str
    description: str
    input: Dict[str, Any] = Field(default_factory=dict)
    expected_behavior: str = ""
    checks: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Verdict(str, Enum):
    """A case that crashes the agent/adapter (ERROR) is a different finding
    than an agent that ran fine but produced output violating an expected
    property (FAIL) - P6 Phase 3 explicitly requires this distinction.

    SKIPPED (P8B.4): the case was never actually attempted against the
    configured provider - either because it is a MOCK_ONLY /
    PROVIDER_FAILURE_CONTRACT case (its purpose is to simulate a scripted
    fake-provider failure a real provider cannot be made to reproduce - see
    evaluation/runner.py's is_mock_only_case) or because the run's
    --max-calls budget was already exhausted. Deliberately distinct from
    FAIL/ERROR: a SKIPPED case says nothing about whether the agent/prompt
    is correct, and must never be counted toward a pass rate."""
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIPPED = "skipped"


class MetricOutcome(BaseModel):
    """The result of one metric check within one case."""
    metric: str
    passed: bool
    detail: str = ""


class EvaluationResult(BaseModel):
    """Result of running one EvaluationCase (P6 Phase 3)."""
    case_id: str
    agent: str
    verdict: Verdict
    metrics: List[MetricOutcome] = Field(default_factory=list)
    failures: List[str] = Field(default_factory=list)
    explanation: str = ""
    # P8B.4: set only when verdict == SKIPPED - "MOCK_ONLY_PROVIDER_FAILURE_CONTRACT"
    # or "BUDGET_EXHAUSTED" (see evaluation/runner.py). None for every other
    # verdict; kept as a separate field (rather than overloading explanation)
    # so callers can bucket SKIPPED results by reason without string parsing.
    skip_reason: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.verdict == Verdict.PASS
