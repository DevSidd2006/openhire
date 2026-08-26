"""
P6: evaluation runner. P8B.1: explicit provider selection.

    python -m evaluation.runner [agent_name ...] [--provider mock|groq|gemini]

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

Provider selection (P8B.1):
  --provider mock (the default - identical to every invocation before
      P8B.1): no network calls. Every adapter builds its own fresh
      ScriptedLLMProvider from the case's `input["script"]` (see
      evaluation/adapters.py) - deterministic, no API key required.
  --provider groq (P8B.3, the PRIMARY real provider): constructs exactly ONE
      real GroqProvider from config.settings' GROQ_API_KEY/GROQ_MODEL
      (openai/gpt-oss-20b) and injects it into every case's adapter call.
      This is the authoritative real-LLM behavioral evaluation - the mock
      run is a regression check, not evidence the real model behaves
      correctly. Fails immediately and explicitly if GROQ_API_KEY is not
      configured - never silently falls back to mock. Never prints the API
      key; only the provider name and configured model.
  --provider gemini: available but no longer primary. Constructs exactly
      ONE real GeminiProvider (from
      config.settings' GEMINI_API_KEY/GEMINI_MODEL, the same configuration
      system every other agent already uses - see providers/llm/__init__.py)
      and injects it into every case's adapter call. Fails immediately and
      explicitly if GEMINI_API_KEY is not configured - never silently falls
      back to mock. The exact same cases, the exact same adapters, the exact
      same agents and BaseAgent retry/validation path run either way - only
      which LLMProvider answers each call changes. Never prints the API key;
      only the provider name and configured model.

Case classification - A/B/C (P8B.4, extended in the P8B behavioral-contract
audit): every case in evaluation/cases/*.json falls into exactly one bucket:

  A. Real LLM behavioral test (the default - no `provider_contract` tag).
     Its checks describe a contract a real, well-behaved model is expected
     to satisfy, and running it under `--provider groq`/`--provider gemini`
     is a meaningful measurement of whether the real model does.
  B. Deterministic production-agent / business-logic test
     (`metadata.provider_contract == "mock_only_business_logic_contract"`).
     Its script exists to trigger a specific piece of AGENT CODE (weight
     re-normalization, severity-by-confidence capping, an out-of-range
     citation's bounds check...) that a real, well-behaved model is very
     unlikely to trigger naturally on its own initiative - e.g. no
     compliant model deliberately emits competency weights that don't sum
     to 1.0, or cites a question number that doesn't exist. The VALUE of
     the test is in exercising that code path deterministically, not in
     measuring real-model behavior.
  C. Provider/infrastructure-failure contract test
     (`metadata.provider_contract == "mock_only_provider_failure_contract"`).
     Its script exists to simulate the PROVIDER itself misbehaving
     (malformed JSON, missing required fields on every retry, a permanent
     auth error) - never something a real, well-behaved model can be
     reliably made to reproduce.

is_mock_only_case() is True for either B or C - see mock_only_skip_reason()
for which one. Under `--provider mock` every case (A, B, and C) runs exactly
as before (unaffected - the original 84/83/0/1 mock baseline is preserved).
Under a real provider, B and C cases are never sent to the API at all
(0 cost) and are reported as SKIPPED/MOCK_ONLY_BUSINESS_LOGIC_CONTRACT or
SKIPPED/MOCK_ONLY_PROVIDER_FAILURE_CONTRACT respectively - never counted as
a pass, fail, or error. Sending a B or C case to a real model would either
fail to trigger the scripted condition at all (wasting a call on a
non-signal) or, worse, get silently miscounted as if it measured real-model
behavior when it does not.

Call budget (P8B.4, --max-calls N): bounds how many real provider calls one
run is allowed to make, so a real-provider benchmark can be split safely
across multiple daily-quota windows instead of risking the whole quota on
one invocation. Counts ACTUAL provider calls (every generate/
generate_structured call, including retries) via BudgetedProvider, not case
count - a case that needs several calls (retries, multi-turn interviews)
consumes budget accordingly. Once exhausted, remaining cases are reported as
SKIPPED/BUDGET_EXHAUSTED (never silently marked PASS or FAIL) and the run
still exits cleanly with a full report of what DID complete. Has no effect
on `--provider mock` (no default behavior change) unless explicitly passed.
`--case-filter SUBSTRING` narrows to case_ids containing SUBSTRING - use it
together with --max-calls to run one deliberately small slice of the golden
set at a time. The existing positional `agents` argument already serves as
the agent-level filter.
"""
import argparse
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
from providers.base import LLMProvider

CASES_DIR = Path(__file__).parent / "cases"
REPORTS_DIR = Path(__file__).parent / "reports"

# P8B.4 / P8B behavioral-contract audit: metadata key a case's `metadata`
# dict carries to declare it bucket B or C (see this module's docstring's
# "Case classification - A/B/C" section) rather than bucket A (the default,
# untagged case).
MOCK_ONLY_CONTRACT_KEY = "provider_contract"
# Bucket C: simulates a scripted PROVIDER failure.
MOCK_ONLY_PROVIDER_FAILURE_VALUE = "mock_only_provider_failure_contract"
# Bucket B: triggers deterministic AGENT/business-logic code a real,
# well-behaved model won't naturally trigger on its own initiative.
MOCK_ONLY_BUSINESS_LOGIC_VALUE = "mock_only_business_logic_contract"
# Backward-compatible alias (P8B.4 named this MOCK_ONLY_CONTRACT_VALUE
# before the B/C split existed) - still refers to bucket C specifically.
MOCK_ONLY_CONTRACT_VALUE = MOCK_ONLY_PROVIDER_FAILURE_VALUE

_MOCK_ONLY_SKIP_REASONS = {
    MOCK_ONLY_PROVIDER_FAILURE_VALUE: "MOCK_ONLY_PROVIDER_FAILURE_CONTRACT",
    MOCK_ONLY_BUSINESS_LOGIC_VALUE: "MOCK_ONLY_BUSINESS_LOGIC_CONTRACT",
}


def is_mock_only_case(case: EvaluationCase) -> bool:
    """True if this case is bucket B or C (evaluation/cases/*.json
    `metadata.provider_contract` is one of the two mock-only values) and
    must never be sent to a real LLM provider - see this module's
    docstring's "Case classification - A/B/C" section."""
    return case.metadata.get(MOCK_ONLY_CONTRACT_KEY) in _MOCK_ONLY_SKIP_REASONS


def mock_only_skip_reason(case: EvaluationCase) -> Optional[str]:
    """The specific SKIPPED reason string for a bucket B or C case (None for
    bucket A). Distinguishes "this simulates a provider failure" from "this
    triggers deterministic business logic" in reporting - see run_case."""
    return _MOCK_ONLY_SKIP_REASONS.get(case.metadata.get(MOCK_ONLY_CONTRACT_KEY))


# P8B.4: distinctive marker embedded in BudgetExhausted's message so it can
# be recognized even after an agent's own broad `except Exception` handler
# (every agents/*/agent.py execute() has one) has already turned it into a
# normal-shaped `{"...": None, "error": str(exc)}` result - see
# _budget_exhausted_message_in / run_case below.
BUDGET_EXHAUSTED_MARKER = "P8B4_CALL_BUDGET_EXHAUSTED"


class BudgetExhausted(Exception):
    """Raised by BudgetedProvider when a real-provider run's --max-calls
    budget is already spent. Deliberately NOT LLMTransientError/
    LLMPermanentError - BaseAgent._call_with_retry only catches those two
    (plus asyncio.TimeoutError/StructuredOutputValidationError), so this
    propagates immediately, without being retried and without silently
    burning more of an already-exhausted budget."""


class CallBudget:
    """Shared, mutable call counter for one run. `max_calls=None` means
    unbounded (the default - no behavior change from pre-P8B.4)."""

    def __init__(self, max_calls: Optional[int]):
        self.max_calls = max_calls
        self.calls_made = 0

    @property
    def exhausted(self) -> bool:
        return self.max_calls is not None and self.calls_made >= self.max_calls


class BudgetedProvider(LLMProvider):
    """Wraps a real LLMProvider so every actual generate()/
    generate_structured() call - including retries BaseAgent issues, and
    every turn of a multi-call adapter - counts against `budget`, and no
    call is made at all once the budget is spent (P8B.4 Phase 7: "count
    actual provider calls, not merely case count")."""

    def __init__(self, delegate: LLMProvider, budget: CallBudget):
        self.delegate = delegate
        self.budget = budget

    def _check(self) -> None:
        if self.budget.exhausted:
            raise BudgetExhausted(
                f"{BUDGET_EXHAUSTED_MARKER}: call budget of {self.budget.max_calls} "
                "already reached - this call was never sent."
            )
        self.budget.calls_made += 1

    async def generate(self, prompt: str, **kwargs) -> str:
        self._check()
        return await self.delegate.generate(prompt, **kwargs)

    async def generate_structured(self, prompt: str, schema, **kwargs):
        self._check()
        return await self.delegate.generate_structured(prompt, schema, **kwargs)


def _budget_exhausted_message_in(ctx: Dict) -> Optional[str]:
    """Best-effort detection of a BudgetExhausted that an agent's own broad
    `except Exception` already swallowed into its normal error-result shape
    (`ctx["result"]` is every agent's raw execute() return dict, which
    always carries an "error" string on failure - see agents/*/agent.py).
    Returns the message if found, else None."""
    result = ctx.get("result") if isinstance(ctx, dict) else None
    if isinstance(result, dict):
        error = result.get("error")
        if isinstance(error, str) and BUDGET_EXHAUSTED_MARKER in error:
            return error
    return None


def load_cases(
    agent_filter: Optional[List[str]] = None, case_filter: Optional[str] = None
) -> List[EvaluationCase]:
    """Load every case from evaluation/cases/*.json. Each file is a JSON
    array of case objects for one agent (filename is informational only -
    each case's own `agent` field is authoritative, matching how the
    adapter registry is keyed).

    `case_filter` (P8B.4, --case-filter): an optional substring match on
    case_id, applied after agent_filter - lets a real-provider run target
    one deliberately small slice of the golden set (e.g. for incremental
    execution across quota windows) without touching agent_filter's
    existing meaning."""
    cases: List[EvaluationCase] = []
    for path in sorted(CASES_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        for entry in raw:
            case = EvaluationCase.model_validate(entry)
            if agent_filter and case.agent not in agent_filter:
                continue
            if case_filter and case_filter not in case.case_id:
                continue
            cases.append(case)
    return cases


async def run_case(case: EvaluationCase, provider: Optional[LLMProvider] = None) -> EvaluationResult:
    """`provider=None` (the default) preserves the exact pre-P8B.1 behavior:
    every adapter builds its own ScriptedLLMProvider. Passing a real
    provider injects it into the SAME adapter call - see
    evaluation/adapters.py's module docstring.

    P8B.4: when `provider` is not None (a real-provider run) and this case
    is_mock_only_case() (bucket B or C), the case is never sent to the API
    at all - see this module's docstring's "Case classification - A/B/C"
    section."""
    skip_reason = mock_only_skip_reason(case) if provider is not None else None
    if skip_reason == "MOCK_ONLY_PROVIDER_FAILURE_CONTRACT":
        return EvaluationResult(
            case_id=case.case_id, agent=case.agent, verdict=Verdict.SKIPPED,
            skip_reason=skip_reason,
            explanation=(
                "This case simulates a scripted provider failure "
                f"({case.metadata.get('category', 'unknown')}) that a real provider "
                "cannot be reliably made to reproduce - never sent to the real API."
            ),
        )
    if skip_reason == "MOCK_ONLY_BUSINESS_LOGIC_CONTRACT":
        return EvaluationResult(
            case_id=case.case_id, agent=case.agent, verdict=Verdict.SKIPPED,
            skip_reason=skip_reason,
            explanation=(
                "This case triggers a deterministic AGENT/business-logic code path "
                f"({case.metadata.get('category', 'unknown')}) that a real, well-behaved "
                "model is unlikely to trigger on its own initiative - never sent to the real API."
            ),
        )

    adapter = ADAPTERS.get(case.agent)
    if adapter is None:
        return EvaluationResult(
            case_id=case.case_id, agent=case.agent, verdict=Verdict.ERROR,
            explanation=f"No adapter registered for agent {case.agent!r}",
        )

    try:
        ctx = await adapter(case, provider)
    except BudgetExhausted as exc:
        return EvaluationResult(
            case_id=case.case_id, agent=case.agent, verdict=Verdict.SKIPPED,
            skip_reason="BUDGET_EXHAUSTED", explanation=str(exc),
        )
    except Exception as exc:
        return EvaluationResult(
            case_id=case.case_id, agent=case.agent, verdict=Verdict.ERROR,
            explanation=f"{type(exc).__name__}: {exc}",
        )

    # The budget may have been exhausted mid-case and swallowed by the
    # agent's own broad except-Exception handler (see
    # _budget_exhausted_message_in's docstring) rather than propagating out
    # of adapter() as a raw BudgetExhausted - detect that shape too, so it is
    # still reported as SKIPPED, never as a fabricated FAIL/ERROR.
    budget_msg = _budget_exhausted_message_in(ctx)
    if budget_msg is not None:
        return EvaluationResult(
            case_id=case.case_id, agent=case.agent, verdict=Verdict.SKIPPED,
            skip_reason="BUDGET_EXHAUSTED", explanation=budget_msg,
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


async def run_all(
    agent_filter: Optional[List[str]] = None,
    provider: Optional[LLMProvider] = None,
    case_filter: Optional[str] = None,
    max_calls: Optional[int] = None,
) -> List[EvaluationResult]:
    """`max_calls=None` (the default) is unbounded - identical to every
    invocation before P8B.4. When given AND a real provider is in play, every
    case's provider calls draw from one shared CallBudget; once it is
    exhausted, every remaining case is skipped up front (0 additional calls)
    rather than started and left to run into the budget wall mid-call - see
    BudgetedProvider/CallBudget above."""
    cases = load_cases(agent_filter, case_filter)

    budget: Optional[CallBudget] = None
    run_provider = provider
    if provider is not None and max_calls is not None:
        budget = CallBudget(max_calls)
        run_provider = BudgetedProvider(provider, budget)

    results: List[EvaluationResult] = []
    for case in cases:
        if budget is not None and budget.exhausted:
            results.append(EvaluationResult(
                case_id=case.case_id, agent=case.agent, verdict=Verdict.SKIPPED,
                skip_reason="BUDGET_EXHAUSTED",
                explanation=(
                    f"Call budget of {budget.max_calls} already reached "
                    f"({budget.calls_made} calls made) - this case was never started."
                ),
            ))
            continue
        results.append(await run_case(case, run_provider))
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_MOCK_ONLY_SKIP_REASON_VALUES = frozenset(_MOCK_ONLY_SKIP_REASONS.values())


def summarize(results: List[EvaluationResult]) -> str:
    # P8B.4 / P8B behavioral-contract audit: SKIPPED (bucket B/C-under-
    # real-provider, or budget-exhausted) cases are reported alongside
    # pass/fail/error but never folded into the pass rate - see
    # Verdict.SKIPPED's docstring. "behavioral" here means "bucket A, plus
    # anything actually attempted" - i.e. everything except bucket B/C.
    behavioral = [r for r in results if r.skip_reason not in _MOCK_ONLY_SKIP_REASON_VALUES]

    by_agent: Dict[str, Counter] = defaultdict(Counter)
    for r in behavioral:
        by_agent[r.agent][r.verdict.value] += 1

    lines = []
    header = f"{'Agent':<24}{'Cases':>7}{'Pass':>7}{'Fail':>7}{'Error':>7}{'Skipped':>9}"
    lines.append(header)
    lines.append("-" * len(header))

    total_cases = total_pass = total_fail = total_error = total_skipped = 0
    for agent in sorted(by_agent):
        counts = by_agent[agent]
        cases = sum(counts.values())
        passed, failed, errored, skipped = counts["pass"], counts["fail"], counts["error"], counts["skipped"]
        total_cases += cases
        total_pass += passed
        total_fail += failed
        total_error += errored
        total_skipped += skipped
        lines.append(f"{agent:<24}{cases:>7}{passed:>7}{failed:>7}{errored:>7}{skipped:>9}")

    lines.append("-" * len(header))
    lines.append(f"{'TOTAL':<24}{total_cases:>7}{total_pass:>7}{total_fail:>7}{total_error:>7}{total_skipped:>9}")

    # Pass rate is computed over ATTEMPTED cases only (excludes every
    # SKIPPED case, budget-exhausted or mock-only) - a SKIPPED case was
    # never actually run and must not silently drag the rate down or up.
    attempted = total_pass + total_fail + total_error
    pass_rate = (total_pass / attempted * 100) if attempted else 0.0
    lines.append("")
    lines.append(f"Overall pass rate: {pass_rate:.1f}% ({total_pass}/{attempted} attempted)")
    if total_skipped:
        lines.append(f"Skipped (not counted toward pass rate): {total_skipped}")

    business_logic_only = [r for r in results if r.skip_reason == "MOCK_ONLY_BUSINESS_LOGIC_CONTRACT"]
    provider_failure_only = [r for r in results if r.skip_reason == "MOCK_ONLY_PROVIDER_FAILURE_CONTRACT"]
    if business_logic_only or provider_failure_only:
        lines.append("")
        lines.append(
            f"A. Real LLM behavioral cases applicable to a real model: {len(behavioral)} "
            f"({total_pass} pass, {total_fail} fail, {total_error} error, "
            f"{total_skipped} skipped-for-budget)"
        )
        lines.append(
            f"B. Deterministic business-logic contract cases (MOCK_ONLY, run via mock only): "
            f"{len(business_logic_only)}"
        )
        lines.append(
            f"C. Provider-failure contract cases (MOCK_ONLY, run via mock only): "
            f"{len(provider_failure_only)}"
        )

    # Most common failure/error categories (metric name -> count). SKIPPED
    # cases are never a "failure category" - they were never attempted.
    metric_failures: Counter = Counter()
    for r in behavioral:
        if r.verdict in (Verdict.FAIL, Verdict.ERROR):
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
        if r.verdict in (Verdict.FAIL, Verdict.ERROR):
            lines.append(f"[{r.verdict.value.upper()}] {r.agent} :: {r.case_id}")
            lines.append(f"    {r.explanation}")
    return "\n".join(lines) if lines else "(no failures)"


def skipped_cases_detail(results: List[EvaluationResult]) -> str:
    lines = []
    for r in results:
        if r.verdict == Verdict.SKIPPED:
            lines.append(f"[SKIPPED/{r.skip_reason}] {r.agent} :: {r.case_id}")
            lines.append(f"    {r.explanation}")
    return "\n".join(lines) if lines else "(no skipped cases)"


def write_report(results: List[EvaluationResult], provider_label: str = "mock") -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"report_{timestamp}.json"
    payload = {
        "generated_at": timestamp,
        "provider": provider_label,  # traceability only - never the key itself
        "results": [r.model_dump() for r in results],
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def build_provider(name: str) -> Optional[LLMProvider]:
    """Resolve a `--provider` CLI value into what run_all()/run_case()
    expect: None for mock (preserves the exact pre-P8B.1 default - each
    adapter builds its own ScriptedLLMProvider), or a single, real,
    constructed LLMProvider instance shared across every case in the run.

    Never prints or returns the API key itself - only the configured model
    name is ever surfaced. Raises SystemExit with a clear, key-free message
    if real-provider credentials are missing - never falls back to mock
    silently.
    """
    if name == "mock":
        print("Provider: mock (ScriptedLLMProvider per case)")
        return None

    if name == "groq":
        from config.settings import GROQ_API_KEY, GROQ_MODEL

        if not GROQ_API_KEY:
            raise SystemExit(
                "GROQ_API_KEY is not configured - cannot run evaluation with "
                "--provider groq. Set it in your .env file first (never in "
                "a tracked file)."
            )
        from providers.llm.groq import GroqProvider

        print("Provider: groq")
        print(f"Model: {GROQ_MODEL}")
        return GroqProvider(api_key=GROQ_API_KEY, model=GROQ_MODEL)

    if name == "gemini":
        from config.settings import GEMINI_API_KEY, GEMINI_MODEL

        if not GEMINI_API_KEY:
            raise SystemExit(
                "GEMINI_API_KEY is not configured - cannot run evaluation with "
                "--provider gemini. Set it in your .env file first (never in "
                "a tracked file)."
            )
        from providers.llm.gemini import GeminiProvider

        print("Provider: gemini")
        print(f"Model: {GEMINI_MODEL}")
        return GeminiProvider(api_key=GEMINI_API_KEY, model=GEMINI_MODEL)

    raise SystemExit(
        f"Unknown --provider {name!r} (expected 'mock', 'groq' or 'gemini')"
    )


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.runner",
        description="Run the golden evaluation dataset against a mock/scripted or real LLM provider.",
    )
    parser.add_argument(
        "agents", nargs="*", default=None,
        help="Optional agent name filter(s), e.g. jd_analyzer technical_evaluator",
    )
    parser.add_argument(
        "--provider", choices=["mock", "groq", "gemini"], default="mock",
        help="Which LLM provider to run agent-calling cases against (default: mock, no network calls)",
    )
    parser.add_argument(
        "--max-calls", type=int, default=None, metavar="N",
        help=(
            "Bound how many real provider calls this run may make (P8B.4). Counts every "
            "actual generate/generate_structured call, including retries - not case count. "
            "Once reached, remaining cases are reported SKIPPED/BUDGET_EXHAUSTED, never "
            "silently marked PASS/FAIL. No effect under --provider mock. Default: unbounded."
        ),
    )
    parser.add_argument(
        "--case-filter", default=None, metavar="SUBSTRING",
        help="Only run cases whose case_id contains SUBSTRING (P8B.4) - combine with --max-calls "
             "for incremental real-provider execution across quota windows.",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = _parse_args()
    provider = build_provider(args.provider)

    agent_filter = args.agents or None
    if args.max_calls is not None:
        print(f"Call budget: {args.max_calls}")
    start = time.monotonic()
    results = asyncio.run(run_all(agent_filter, provider, case_filter=args.case_filter, max_calls=args.max_calls))
    elapsed = time.monotonic() - start

    print()
    print(summarize(results))
    print()
    print("Failing/errored cases:")
    print(failing_cases_detail(results))
    if any(r.verdict == Verdict.SKIPPED for r in results):
        print()
        print("Skipped cases:")
        print(skipped_cases_detail(results))

    report_path = write_report(results, provider_label=args.provider)
    print()
    print(f"Full report written to {report_path}")
    print(f"Completed in {elapsed:.2f}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
