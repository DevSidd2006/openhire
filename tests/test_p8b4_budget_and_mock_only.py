"""P8B.4: regression tests for the two evaluation/runner.py additions
Phase 6 (MOCK_ONLY / PROVIDER_FAILURE_CONTRACT classification) and Phase 7/8
(--max-calls call budget) needed before a real Groq benchmark could be run
safely and honestly. All offline - `provider` here is always a
ScriptedLLMProvider standing in for "some real, injected LLMProvider" (the
same convention tests/test_evaluation_provider_injection.py uses), never an
actual network call.
"""
import json

import pytest

from evaluation.models import Verdict
from evaluation.runner import (
    CallBudget,
    is_mock_only_case,
    load_cases,
    mock_only_skip_reason,
    run_all,
    run_case,
)
from tests.fakes import ScriptedLLMProvider


def _case(case_id: str):
    return next(c for c in load_cases() if c.case_id == case_id)


# ---------------------------------------------------------------------------
# Phase 6: MOCK_ONLY / PROVIDER_FAILURE_CONTRACT classification
# ---------------------------------------------------------------------------

class TestMockOnlyClassification:
    @pytest.mark.parametrize("case_id", [
        "jd_malformed_structured_output",
        "resume_malformed_llm_output_fallback",
        "resume_permanent_provider_error_falls_back",
        "interviewer_evaluate_malformed_output_errors_explicitly",
    ])
    def test_known_provider_failure_cases_are_classified_mock_only(self, case_id):
        assert is_mock_only_case(_case(case_id)) is True

    def test_normal_case_is_not_mock_only(self):
        assert is_mock_only_case(_case("jd_conflicting_requirements")) is False


class TestBucketBBusinessLogicClassification:
    """P8B behavioral-contract audit (Batch 1 follow-up): three cases
    trigger deterministic AGENT/business-logic code paths (weight
    normalization, severity-by-confidence capping, an out-of-range
    citation's bounds check) that a real, well-behaved model is very
    unlikely to trigger on its own initiative - classified bucket B
    (MOCK_ONLY_BUSINESS_LOGIC_CONTRACT), distinct from bucket C
    (MOCK_ONLY_PROVIDER_FAILURE_CONTRACT, simulated provider failures)."""

    @pytest.mark.parametrize("case_id", [
        "jd_weight_normalization",
        "integrity_weak_evidence_must_not_become_high_severity",
        "behav_out_of_range_citation_never_grounded",
        "tech_out_of_range_citation_never_grounded",
        "audit_ambiguous_evidence_demoted_to_review",
        # P8B.5: both gated on MIN_CONFIDENCE_FOR_COVERAGE being cleared by
        # the MODEL's self-reported confidence, which openai/gpt-oss-20b
        # calibrates conservatively and stochastically - proven
        # non-deterministic across 4 identical live runs. Testing the
        # deterministic threshold logic requires controlled confidence.
        "interviewer_adaptive_sequence_differs_by_answer_quality",
        "interviewer_adaptive_termination_sufficient_evidence",
    ])
    def test_known_business_logic_cases_are_classified_bucket_b(self, case_id):
        case = _case(case_id)
        assert is_mock_only_case(case) is True
        assert mock_only_skip_reason(case) == "MOCK_ONLY_BUSINESS_LOGIC_CONTRACT"

    @pytest.mark.parametrize("case_id", [
        "jd_malformed_structured_output",
        "resume_permanent_provider_error_falls_back",
    ])
    def test_bucket_c_cases_still_classify_as_provider_failure_not_business_logic(self, case_id):
        assert mock_only_skip_reason(_case(case_id)) == "MOCK_ONLY_PROVIDER_FAILURE_CONTRACT"

    def test_behav_irrelevant_answer_stays_bucket_a_real_llm_behavioral(self):
        """This case looked similar to the bucket-B cases (it also failed
        against real Groq in Batch 1) but its failure was real, valid model
        variance on a genuine behavioral contract (see
        tests/test_p8b3_groq_agent_fixes.py::
        TestOfftopicAnswerWithValidCitationIsSupportedNotInsufficient) - NOT
        a scripted-only scenario. It must remain bucket A (untagged)."""
        case = _case("behav_irrelevant_answer_not_positive_evidence")
        assert is_mock_only_case(case) is False
        assert mock_only_skip_reason(case) is None

    @pytest.mark.asyncio
    async def test_bucket_b_case_under_real_provider_is_skipped_not_sent(self):
        case = _case("jd_weight_normalization")
        provider = ScriptedLLMProvider(script=[json.dumps({"title": "should never be used"})])

        result = await run_case(case, provider=provider)

        assert result.verdict == Verdict.SKIPPED
        assert result.skip_reason == "MOCK_ONLY_BUSINESS_LOGIC_CONTRACT"
        assert len(provider.calls) == 0

    @pytest.mark.asyncio
    async def test_bucket_b_case_under_mock_provider_runs_normally(self):
        """provider=None (mock mode) is completely unaffected - unchanged
        pre-P8B behavior for every bucket-B case."""
        case = _case("jd_weight_normalization")
        result = await run_case(case, provider=None)
        assert result.verdict == Verdict.PASS
        assert result.skip_reason is None

    @pytest.mark.asyncio
    async def test_mock_only_case_under_mock_provider_runs_normally(self):
        """provider=None (mock mode) is completely unaffected - the original
        84/83/0/1 mock baseline must not change because of this feature."""
        case = _case("jd_malformed_structured_output")
        result = await run_case(case, provider=None)
        # The case's own checks assert output is None and result.error is
        # populated - the scripted malformed-JSON failure IS the expected,
        # checked outcome, so a correctly-behaving run is PASS, not ERROR
        # (unchanged pre-P8B.4 behavior; see evaluation/cases/jd_analyzer.json).
        assert result.verdict == Verdict.PASS
        assert result.skip_reason is None

    @pytest.mark.asyncio
    async def test_mock_only_case_under_real_provider_is_skipped_not_sent(self):
        """A real (here: scripted-standing-in-for-real) provider must never
        actually be called for a MOCK_ONLY case."""
        case = _case("jd_malformed_structured_output")
        provider = ScriptedLLMProvider(script=[json.dumps({"title": "should never be used"})])

        result = await run_case(case, provider=provider)

        assert result.verdict == Verdict.SKIPPED
        assert result.skip_reason == "MOCK_ONLY_PROVIDER_FAILURE_CONTRACT"
        assert len(provider.calls) == 0  # never actually called

    @pytest.mark.asyncio
    async def test_mock_only_skip_is_never_counted_as_fail_or_error(self):
        """Do NOT count a real-provider run as failing simply because the
        real provider did not obey a scripted fake response (P8B.4 Phase 6)."""
        case = _case("resume_permanent_provider_error_falls_back")
        provider = ScriptedLLMProvider(script=[json.dumps({"skills": ["Python"]})])

        result = await run_case(case, provider=provider)

        assert result.verdict not in (Verdict.FAIL, Verdict.ERROR)
        assert result.verdict == Verdict.SKIPPED


# ---------------------------------------------------------------------------
# Phase 7/8: call budget
# ---------------------------------------------------------------------------

class TestCallBudget:
    def test_unbounded_budget_never_exhausted(self):
        budget = CallBudget(max_calls=None)
        budget.calls_made = 10_000
        assert budget.exhausted is False

    def test_bounded_budget_exhausts_at_limit(self):
        budget = CallBudget(max_calls=2)
        assert budget.exhausted is False
        budget.calls_made = 2
        assert budget.exhausted is True

    @pytest.mark.asyncio
    async def test_max_calls_none_is_unbounded_default_behavior_unchanged(self):
        """No --max-calls given -> identical to pre-P8B.4: every case in the
        filter runs, regardless of how many calls it needs."""
        provider = ScriptedLLMProvider(script=[json.dumps({"title": "T", "competencies": []})])
        results = await run_all(["jd_analyzer"], provider=provider, max_calls=None)
        assert all(r.skip_reason != "BUDGET_EXHAUSTED" for r in results)

    @pytest.mark.asyncio
    async def test_max_calls_stops_cleanly_and_skips_remaining_cases(self):
        """jd_analyzer has 8 cases (1 real call each, once mock-only cases
        are excluded from actually calling the provider). A budget of 2
        must attempt at most 2 real calls and mark the rest SKIPPED/
        BUDGET_EXHAUSTED - never silently PASS or FAIL them."""
        provider = ScriptedLLMProvider(script=[json.dumps({"title": "T", "competencies": []})])
        results = await run_all(["jd_analyzer"], provider=provider, max_calls=2)

        assert len(provider.calls) <= 2
        skipped = [r for r in results if r.verdict == Verdict.SKIPPED]
        assert any(r.skip_reason == "BUDGET_EXHAUSTED" for r in skipped)
        # Every result is accounted for - none silently dropped.
        assert len(results) == len(load_cases(["jd_analyzer"]))
        # No skipped-for-budget case was fabricated as PASS or FAIL.
        for r in results:
            if r.skip_reason == "BUDGET_EXHAUSTED":
                assert r.verdict == Verdict.SKIPPED

    @pytest.mark.asyncio
    async def test_budget_counts_actual_calls_not_case_count(self):
        """A budget of exactly 1 must permit only 1 real call being made,
        even though the filtered case set has many cases - proves the
        counter is calls-based, not len(cases)-based."""
        provider = ScriptedLLMProvider(script=[json.dumps({"title": "T", "competencies": []})])
        results = await run_all(["jd_analyzer"], provider=provider, max_calls=1)

        assert len(provider.calls) == 1
        skipped_for_budget = [r for r in results if r.skip_reason == "BUDGET_EXHAUSTED"]
        assert len(skipped_for_budget) >= 1

    @pytest.mark.asyncio
    async def test_case_filter_narrows_case_set(self):
        results = await run_all(None, provider=None, case_filter="jd_conflicting")
        assert len(results) == 1
        assert results[0].case_id == "jd_conflicting_requirements"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
