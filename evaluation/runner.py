"""
P6: evaluation runner.

    python -m evaluation.runner [agent_name ...]

Loads every case in evaluation/cases/*.json, runs each through its agent's
adapter (evaluation/adapters.py), checks the declared metrics
(evaluation/metrics.py), and prints a per-agent PASS/FAIL/ERROR table plus a
summary of the most common failure categories. Also writes a timestamped
JSON report to evaluation/reports/ with the full per-case detail, so a
failure can be inspected after the fact without re-running everything.

Design intent (P6 Phase 24): this runner NEVER modifies a case to make it
pass. A FAIL is a real, recorded finding - see docs/multi-agent-system.md's
"Agent Evaluation Framework" section and the P6 final report for how
failures found here were triaged (fixed vs. documented as a known gap).

Runs entirely in mock/deterministic mode - every case's adapter constructs
its own ScriptedLLMProvider (see evaluation/adapters.py's module docstring)
or calls a purely-deterministic agent; nothing here calls a real LLM or
requires an API key (P6 Phase 26).
"""
import asyncio
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from evaluation.adapters import ADAPTERS
from evaluation.metrics import run_metric
from evaluation.models import EvaluationCase, EvaluationResult, MetricOutcome, Verdict

CASES_DIR = Path(__file__).parent / "cases"
REPORTS_DIR = Path(__file__).parent / "reports"


def load_cases(agent_filter: Optional[List[str]] = None) -> List[EvaluationCase]:
    """Load every case from evaluation/cases/*.json. Each file is a JSON
    array of case objects for one agent (filename is informational only -
    each case's own `agent` field is authoritative, matching how the
    adapter registry is keyed)."""
    cases: List[EvaluationCase] = []
    for path in sorted(CASES_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        for entry in raw:
            case = EvaluationCase.model_validate(entry)
            if agent_filter and case.agent not in agent_filter:
                continue
            cases.append(case)
    return cases


async def run_case(case: EvaluationCase) -> EvaluationResult:
    adapter = ADAPTERS.get(case.agent)
    if adapter is None:
        return EvaluationResult(
            case_id=case.case_id, agent=case.agent, verdict=Verdict.ERROR,
            explanation=f"No adapter registered for agent {case.agent!r}",
        )

    try:
        ctx = await adapter(case)
    except Exception as exc:
        return EvaluationResult(
            case_id=case.case_id, agent=case.agent, verdict=Verdict.ERROR,
            explanation=f"{type(exc).__name__}: {exc}",
        )

    outcomes = []
    for check in case.checks:
        try:
            outcomes.append(run_metric(case, ctx, check))
        except Exception as exc:
            outcomes.append(MetricOutcome(
                metric=check.get("metric", "unknown"), passed=False,
                detail=f"metric crashed: {type(exc).__name__}: {exc}",
            ))

    failures = [o.metric for o in outcomes if not o.passed]
    verdict = Verdict.PASS if not failures else Verdict.FAIL
    return EvaluationResult(
        case_id=case.case_id, agent=case.agent, verdict=verdict,
        metrics=outcomes, failures=failures,
        explanation="" if verdict == Verdict.PASS else "; ".join(
            f"{o.metric}: {o.detail}" for o in outcomes if not o.passed
        ),
    )


async def run_all(agent_filter: Optional[List[str]] = None) -> List[EvaluationResult]:
    cases = load_cases(agent_filter)
    return [await run_case(case) for case in cases]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def summarize(results: List[EvaluationResult]) -> str:
    by_agent: Dict[str, Counter] = defaultdict(Counter)
    for r in results:
        by_agent[r.agent][r.verdict.value] += 1

    lines = []
    header = f"{'Agent':<24}{'Cases':>7}{'Pass':>7}{'Fail':>7}{'Error':>7}"
    lines.append(header)
    lines.append("-" * len(header))

    total_cases = total_pass = total_fail = total_error = 0
    for agent in sorted(by_agent):
        counts = by_agent[agent]
        cases = sum(counts.values())
        passed, failed, errored = counts["pass"], counts["fail"], counts["error"]
        total_cases += cases
        total_pass += passed
        total_fail += failed
        total_error += errored
        lines.append(f"{agent:<24}{cases:>7}{passed:>7}{failed:>7}{errored:>7}")

    lines.append("-" * len(header))
    lines.append(f"{'TOTAL':<24}{total_cases:>7}{total_pass:>7}{total_fail:>7}{total_error:>7}")

    pass_rate = (total_pass / total_cases * 100) if total_cases else 0.0
    lines.append("")
    lines.append(f"Overall pass rate: {pass_rate:.1f}% ({total_pass}/{total_cases})")

    # Most common failure/error categories (metric name -> count).
    metric_failures: Counter = Counter()
    for r in results:
        if r.verdict != Verdict.PASS:
            for f in r.failures:
                metric_failures[f] += 1
            if r.verdict == Verdict.ERROR:
                metric_failures["<adapter error>"] += 1

    if metric_failures:
        lines.append("")
        lines.append("Most common failure categories:")
        for metric, count in metric_failures.most_common(10):
            lines.append(f"  {metric:<40}{count}")

    return "\n".join(lines)


def failing_cases_detail(results: List[EvaluationResult]) -> str:
    lines = []
    for r in results:
        if r.verdict != Verdict.PASS:
            lines.append(f"[{r.verdict.value.upper()}] {r.agent} :: {r.case_id}")
            lines.append(f"    {r.explanation}")
    return "\n".join(lines) if lines else "(no failures)"


def write_report(results: List[EvaluationResult]) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"report_{timestamp}.json"
    payload = {
        "generated_at": timestamp,
        "results": [r.model_dump() for r in results],
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def main() -> int:
    agent_filter = sys.argv[1:] or None
    start = time.monotonic()
    results = asyncio.run(run_all(agent_filter))
    elapsed = time.monotonic() - start

    print(summarize(results))
    print()
    print("Failing/errored cases:")
    print(failing_cases_detail(results))

    report_path = write_report(results)
    print()
    print(f"Full report written to {report_path}")
    print(f"Completed in {elapsed:.2f}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
