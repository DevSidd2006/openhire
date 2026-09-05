"""
P6 Phase 4: deterministic metric framework.

Every metric here is a plain Python function - no LLM judge, matching the
spec's "prefer deterministic metrics first" and "everything must work in
mock/deterministic mode" (P6 Phase 26). Each metric takes
(case, ctx, check) and returns a MetricOutcome. `ctx` is whatever the
agent's adapter (evaluation/adapters.py) produced - always a dict with at
least a `"result"` key (the raw dict the agent's execute() returned) and
usually an `"output"` key (the primary pydantic object extracted from it).

Adding a metric = adding one function + one registry entry. A case's
`checks` list references metrics by name plus whatever parameters that
metric needs (see evaluation/cases/*.json), so new checks never require
touching this file's callers.
"""
import re
from typing import Any, Callable, Dict, List, Optional

from evaluation.grounding import check_evidence_grounding
from evaluation.models import EvaluationCase, MetricOutcome

MetricFn = Callable[[EvaluationCase, Dict[str, Any], Dict[str, Any]], MetricOutcome]

_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def resolve_path(obj: Any, path: str) -> Any:
    """Resolve a dotted/indexed path like "output.competencies[0].name"
    against a dict-or-object graph. Returns None if any hop is missing -
    metrics treat that as "value absent", not a crash."""
    if not path:
        return obj
    current = obj
    for name, idx in _TOKEN_RE.findall(path):
        if current is None:
            return None
        if name:
            if isinstance(current, dict):
                current = current.get(name)
            else:
                current = getattr(current, name, None)
        else:
            i = int(idx)
            try:
                current = current[i]
            except (IndexError, TypeError, KeyError):
                current = None
    return current


def _outcome(metric: str, passed: bool, detail: str = "") -> MetricOutcome:
    return MetricOutcome(metric=metric, passed=passed, detail=detail)


# ---------------------------------------------------------------------------
# Generic structural metrics
# ---------------------------------------------------------------------------

def m_no_error(case, ctx, check) -> MetricOutcome:
    """The agent's own execute() reported no soft error (the
    {"...": None, "error": "..."} pattern every agent in this codebase
    uses on failure)."""
    error = ctx.get("result", {}).get("error")
    return _outcome("no_error", error is None, detail=str(error) if error else "")


def m_schema_validity(case, ctx, check) -> MetricOutcome:
    """The primary output object exists (implies it already passed pydantic
    validation - a schema-invalid object could never have been constructed)."""
    output = ctx.get("output")
    return _outcome("schema_validity", output is not None, "" if output is not None else "no output object produced")


def m_field_nonempty(case, ctx, check) -> MetricOutcome:
    field = check["field"]
    value = resolve_path(ctx, field)
    ok = value is not None and value != "" and value != [] and value != {}
    return _outcome("field_nonempty", ok, f"{field} = {value!r}")


def m_field_equals(case, ctx, check) -> MetricOutcome:
    field = check["field"]
    expected = check["value"]
    actual = resolve_path(ctx, field)
    return _outcome("field_equals", actual == expected, f"{field}: expected {expected!r}, got {actual!r}")


def m_field_in_range(case, ctx, check) -> MetricOutcome:
    field = check["field"]
    lo, hi = check.get("min", float("-inf")), check.get("max", float("inf"))
    value = resolve_path(ctx, field)
    ok = isinstance(value, (int, float)) and lo <= value <= hi
    return _outcome("field_in_range", ok, f"{field} = {value!r}, expected [{lo}, {hi}]")


def m_list_contains(case, ctx, check) -> MetricOutcome:
    field = check["field"]
    value = check["value"]
    items = resolve_path(ctx, field) or []
    ok = any(str(v).strip().lower() == str(value).strip().lower() for v in items)
    return _outcome("list_contains", ok, f"{field} = {items!r}, expected to contain {value!r}")


def m_list_not_contains(case, ctx, check) -> MetricOutcome:
    """The fabrication guard: a list field must NOT contain a given value -
    e.g. a skill never mentioned in the source JD/resume text (P6 Phase 6/7:
    "do not reward fabricated information")."""
    field = check["field"]
    forbidden = check.get("values", [check.get("value")])
    items = resolve_path(ctx, field) or []
    items_lower = {str(v).strip().lower() for v in items}
    hit = [v for v in forbidden if str(v).strip().lower() in items_lower]
    return _outcome("list_not_contains", not hit, f"{field} unexpectedly contains {hit!r}" if hit else "")


def m_list_subset_of(case, ctx, check) -> MetricOutcome:
    """Every item in `field` must appear in `allowed` (case-insensitive) -
    a general-purpose "nothing fabricated beyond what's allowed" check."""
    field = check["field"]
    allowed = {str(v).strip().lower() for v in check.get("allowed", [])}
    items = resolve_path(ctx, field) or []
    extra = [v for v in items if str(v).strip().lower() not in allowed]
    return _outcome("list_subset_of", not extra, f"{field} contains unexpected items {extra!r}" if extra else "")


def m_contains_text(case, ctx, check) -> MetricOutcome:
    field = check["field"]
    needle = check["value"].lower()
    value = str(resolve_path(ctx, field) or "").lower()
    return _outcome("contains_text", needle in value, f"{field} does not contain {needle!r}")


def m_not_contains_text(case, ctx, check) -> MetricOutcome:
    field = check["field"]
    needle = check["value"].lower()
    value = str(resolve_path(ctx, field) or "").lower()
    return _outcome("not_contains_text", needle not in value, f"{field} unexpectedly contains {needle!r}")


def m_field_one_of(case, ctx, check) -> MetricOutcome:
    field = check["field"]
    allowed = check["values"]
    value = resolve_path(ctx, field)
    return _outcome("field_one_of", value in allowed, f"{field} = {value!r}, expected one of {allowed!r}")


def m_length_equals(case, ctx, check) -> MetricOutcome:
    field = check["field"]
    expected = check["value"]
    value = resolve_path(ctx, field)
    actual_len = len(value) if value is not None else None
    return _outcome("length_equals", actual_len == expected, f"len({field}) = {actual_len}, expected {expected}")


def m_length_at_most(case, ctx, check) -> MetricOutcome:
    field = check["field"]
    limit = check["value"]
    value = resolve_path(ctx, field)
    actual_len = len(value) if value is not None else 0
    return _outcome("length_at_most", actual_len <= limit, f"len({field}) = {actual_len}, expected <= {limit}")


# ---------------------------------------------------------------------------
# Fact-based metrics - for property-style checks an adapter computes itself
# (e.g. "job_fit changed but weighted_final_score did not") rather than
# something a single field-path lookup can express (P6 Phase 8/15/19/21).
# ---------------------------------------------------------------------------

def m_fact_true(case, ctx, check) -> MetricOutcome:
    fact = check["fact"]
    value = ctx.get("facts", {}).get(fact)
    return _outcome(f"fact:{fact}", value is True, f"{fact} = {value!r}")


def m_fact_false(case, ctx, check) -> MetricOutcome:
    fact = check["fact"]
    value = ctx.get("facts", {}).get(fact)
    return _outcome(f"fact:{fact}", value is False, f"{fact} = {value!r}")


def m_fact_equals(case, ctx, check) -> MetricOutcome:
    fact = check["fact"]
    expected = check["value"]
    value = ctx.get("facts", {}).get(fact)
    return _outcome(f"fact:{fact}", value == expected, f"{fact} = {value!r}, expected {expected!r}")


# ---------------------------------------------------------------------------
# Evidence grounding (P6 Phase 18 - the cross-agent invariant)
# ---------------------------------------------------------------------------

def m_evidence_grounded(case, ctx, check) -> MetricOutcome:
    """Every EvidenceItem reachable from `field` (a list of EvidenceItem, or
    an object/list of objects with an `.evidence` list) must: reference a
    real question in the given transcript, belong to the expected
    candidate, and carry non-empty verbatim text. See
    evaluation/grounding.py for the reusable implementation this project-
    wide invariant is built on (P6 Phase 18)."""
    field = check.get("field", "output")
    transcript = ctx.get("transcript")
    candidate_id = check.get("candidate_id") or ctx.get("candidate_id")
    job_id = check.get("job_id") or ctx.get("job_id")

    target = resolve_path(ctx, field)
    violations = check_evidence_grounding(target, transcript=transcript, candidate_id=candidate_id, job_id=job_id)
    return _outcome("evidence_grounded", not violations, "; ".join(violations) if violations else "")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

METRICS: Dict[str, MetricFn] = {
    "no_error": m_no_error,
    "schema_validity": m_schema_validity,
    "field_nonempty": m_field_nonempty,
    "field_equals": m_field_equals,
    "field_in_range": m_field_in_range,
    "list_contains": m_list_contains,
    "list_not_contains": m_list_not_contains,
    "list_subset_of": m_list_subset_of,
    "contains_text": m_contains_text,
    "not_contains_text": m_not_contains_text,
    "field_one_of": m_field_one_of,
    "length_equals": m_length_equals,
    "length_at_most": m_length_at_most,
    "fact_true": m_fact_true,
    "fact_false": m_fact_false,
    "fact_equals": m_fact_equals,
    "evidence_grounded": m_evidence_grounded,
}


def run_metric(case: EvaluationCase, ctx: Dict[str, Any], check: Dict[str, Any]) -> MetricOutcome:
    name = check.get("metric")
    fn = METRICS.get(name)
    if fn is None:
        return _outcome(name or "unknown", False, f"unknown metric {name!r}")
    return fn(case, ctx, check)
