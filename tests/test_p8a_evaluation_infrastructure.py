"""
P8A: evaluation infrastructure audit/hardening tests.

P7 fixed ScriptedLLMProvider.generate_structured() to classify malformed
JSON as LLMTransientError (retryable), matching the real OpenAIProvider,
instead of letting a raw json.JSONDecodeError bypass BaseAgent's retry
entirely. This file proves, explicitly and in one place:

    1. The full retry contract (P8A Phase 4, cases A-F) holds for
       ScriptedLLMProvider specifically - the fake under audit - using
       genuinely malformed JSON syntax (not just schema-invalid JSON,
       which was already covered before P7).
    2. The evaluation framework's PASS/FAIL/ERROR distinction is real:
       a provider failure the AGENT handles gracefully is a PASS (checked
       against the agent's documented graceful-degradation behavior), a
       provider failure the agent does NOT catch is an ERROR (never FAIL,
       never PASS), and an agent that runs fine but violates an expected
       property is a FAIL (P8A Phase 8).
    3. The cross-agent evidence-grounding sweep (evaluation/grounding.py)
       actually detects corrupted evidence - wrong question_id, wrong
       candidate_id, wrong job_id, altered text, a reference to an answer
       that doesn't exist (P8A Phase 9).

No real LLM, no API key, no network call anywhere in this file.
"""
import json

import pytest

from agents.base import BaseAgent
from agents.jd_analyzer.agent import JDAnalyzerAgent
from evaluation.models import EvaluationCase, Verdict
from evaluation.runner import load_cases, run_case
from evaluation.grounding import check_evidence_grounding
from providers.base import LLMPermanentError
from schemas.evaluation import EvidenceItem, TechnicalEvaluation
from schemas.interview import InterviewAnswer, InterviewQuestion, InterviewTranscript
from schemas.llm_outputs import TechnicalEvaluationResult
from tests.fakes import ScriptedLLMProvider


class _ConcreteAgent(BaseAgent):
    """Minimal concrete BaseAgent - exercises the shared retry/validation
    machinery directly, the same pattern tests/test_retry.py and
    tests/test_structured_output_provider.py already use."""
    async def execute(self, **kwargs):
        return {}


def _tech_schema():
    return TechnicalEvaluationResult.model_json_schema()


# ---------------------------------------------------------------------------
# Phase 4: retry contract, cases A-F, using ScriptedLLMProvider with
# genuinely malformed JSON SYNTAX (the exact class of input the P7 fix
# changed the handling of).
# ---------------------------------------------------------------------------

class TestRetryContractWithMalformedJsonSyntax:
    @pytest.mark.asyncio
    async def test_case_a_first_malformed_json_second_valid_succeeds_after_retry(self):
        fake = ScriptedLLMProvider(script=["not json at all", json.dumps({"technical_score": 7.0})])
        agent = _ConcreteAgent(name="test", llm_provider=fake)

        result = await agent.call_llm_structured(
            "prompt", schema=_tech_schema(), validate=TechnicalEvaluationResult.model_validate,
            max_retries=3, timeout_seconds=1.0,
        )
        assert result.technical_score == 7.0
        assert len(fake.calls) == 2

    @pytest.mark.asyncio
    async def test_case_b_first_invalid_schema_second_valid_succeeds_after_retry(self):
        """Valid JSON, but fails Pydantic validation (score out of range) -
        distinct failure mode from case A (JSON-syntax malformed)."""
        bad = json.dumps({"technical_score": 999.0})
        good = json.dumps({"technical_score": 6.5})
        fake = ScriptedLLMProvider(script=[bad, good])
        agent = _ConcreteAgent(name="test", llm_provider=fake)

        result = await agent.call_llm_structured(
            "prompt", schema=_tech_schema(), validate=TechnicalEvaluationResult.model_validate,
            max_retries=3, timeout_seconds=1.0,
        )
        assert result.technical_score == 6.5
        assert len(fake.calls) == 2

    @pytest.mark.asyncio
    async def test_case_c_all_attempts_malformed_json_explicit_failure(self):
        fake = ScriptedLLMProvider(script=["still not json"])
        agent = _ConcreteAgent(name="test", llm_provider=fake)

        with pytest.raises(RuntimeError, match=r"failed after 3 attempt"):
            await agent.call_llm_structured(
                "prompt", schema=_tech_schema(), validate=TechnicalEvaluationResult.model_validate,
                max_retries=3, timeout_seconds=1.0,
            )
        assert len(fake.calls) == 3  # bounded - no retry storm

    @pytest.mark.asyncio
    async def test_case_d_permanent_provider_error_fails_immediately_no_retries(self):
        fake = ScriptedLLMProvider(script=[LLMPermanentError("invalid api key")])
        agent = _ConcreteAgent(name="test", llm_provider=fake)

        with pytest.raises(LLMPermanentError):
            await agent.call_llm_structured(
                "prompt", schema=_tech_schema(), validate=TechnicalEvaluationResult.model_validate,
                max_retries=3, timeout_seconds=1.0,
            )
        assert len(fake.calls) == 1  # never retried

    @pytest.mark.asyncio
    async def test_case_e_timeout_bounded_retry_then_explicit_failure(self):
        fake = ScriptedLLMProvider(script=["never reached"], hang_seconds=5.0)
        agent = _ConcreteAgent(name="test", llm_provider=fake)

        with pytest.raises(RuntimeError, match=r"failed after 2 attempt"):
            await agent.call_llm_structured(
                "prompt", schema=_tech_schema(), validate=TechnicalEvaluationResult.model_validate,
                max_retries=2, timeout_seconds=0.05,
            )
        assert len(fake.calls) == 2

    @pytest.mark.asyncio
    async def test_case_f_successful_response_exactly_one_provider_call(self):
        fake = ScriptedLLMProvider(script=[json.dumps({"technical_score": 8.0})])
        agent = _ConcreteAgent(name="test", llm_provider=fake)

        result = await agent.call_llm_structured(
            "prompt", schema=_tech_schema(), validate=TechnicalEvaluationResult.model_validate,
            max_retries=3, timeout_seconds=1.0,
        )
        assert result.technical_score == 8.0
        assert len(fake.calls) == 1  # no wasted retries on a clean success


# ---------------------------------------------------------------------------
# Phase 8: PASS vs FAIL vs ERROR
# ---------------------------------------------------------------------------

class TestPassFailErrorDistinction:
    @pytest.mark.asyncio
    async def test_pass_when_agent_behaves_correctly(self):
        cases = {c.case_id: c for c in load_cases(["technical_evaluator"])}
        result = await run_case(cases["tech_strong_answer_grounded"])
        assert result.verdict == Verdict.PASS

    @pytest.mark.asyncio
    async def test_fail_when_agent_runs_fine_but_violates_expected_property(self):
        """Same real case, same real agent, same real (valid) simulated LLM
        output - but the case now asserts something FALSE about that real,
        successfully-produced output. The agent did not crash (no ERROR);
        it just didn't do what was expected (FAIL)."""
        real_case = next(c for c in load_cases(["jd_analyzer"]) if c.case_id == "jd_normal_rich")
        wrong_expectation = real_case.model_copy(update={
            "case_id": "synthetic_wrong_expectation",
            "checks": [{"metric": "field_equals", "field": "output.title", "value": "This Title Was Never Extracted"}],
        })
        result = await run_case(wrong_expectation)
        assert result.verdict == Verdict.FAIL
        assert "field_equals" in result.failures

    @pytest.mark.asyncio
    async def test_error_when_agent_raises_uncaught(self):
        """interviewer_evaluate has no try/except of its own around
        call_llm_structured (agents/interviewer/agent.py:evaluate_answer) -
        a RuntimeError from exhausted retries propagates straight out of
        the adapter, which run_case() must classify as ERROR, never FAIL
        or PASS."""
        cases = {c.case_id: c for c in load_cases(["interviewer_evaluate"])}
        result = await run_case(cases["interviewer_evaluate_malformed_output_errors_explicitly"])
        assert result.verdict == Verdict.ERROR
        assert result.verdict != Verdict.FAIL
        assert result.verdict != Verdict.PASS

    @pytest.mark.asyncio
    async def test_provider_failure_the_agent_catches_is_pass_not_error(self):
        """jd_analyzer's own except Exception catches the exhausted-retry
        RuntimeError and returns job_description=None, error=... - this is
        the agent behaving EXACTLY per its documented contract (explicit
        failure, never fabricated), so the case that checks for exactly
        that must be a PASS, not an ERROR - the adapter never raised."""
        cases = {c.case_id: c for c in load_cases(["jd_analyzer"])}
        result = await run_case(cases["jd_malformed_structured_output"])
        assert result.verdict == Verdict.PASS

    @pytest.mark.asyncio
    async def test_error_case_never_silently_reports_zero_failures_as_pass(self):
        """An ERROR result must never be mistaken for a clean PASS by code
        that only checks `not result.failures` - verdict is the only
        correct source of truth."""
        cases = {c.case_id: c for c in load_cases(["interviewer_evaluate"])}
        result = await run_case(cases["interviewer_evaluate_malformed_output_errors_explicitly"])
        assert result.verdict == Verdict.ERROR
        assert result.explanation  # the real cause is recorded, not silently dropped


# ---------------------------------------------------------------------------
# Phase 9: evidence-grounding regression with deliberately corrupted evidence
# ---------------------------------------------------------------------------

def _real_transcript():
    return InterviewTranscript(
        interview_id="int_1", candidate_id="cand_real", job_id="job_real",
        start_time="t0", is_sealed=True,
        exchanges=[(
            InterviewQuestion(question_id="q1", question_text="Tell us about your Python experience.", category="technical"),
            InterviewAnswer(question_id="q1", answer_text="I built an async FastAPI service."),
        )],
    )


def _real_evidence(**overrides):
    base = dict(
        evidence_id="ev_1", candidate_id="cand_real", source_type="transcript",
        question_id="q1", answer_id="q1", text="I built an async FastAPI service.",
        relevance=0.9, agent="technical_evaluator", explanation="grounded",
    )
    base.update(overrides)
    return EvidenceItem(**base)


class TestEvidenceGroundingRegressionWithCorruptedEvidence:
    def test_genuine_evidence_has_no_violations(self):
        violations = check_evidence_grounding(
            _real_evidence(), transcript=_real_transcript(), candidate_id="cand_real", job_id="job_real",
        )
        assert violations == []

    def test_wrong_question_id_is_a_violation(self):
        corrupted = _real_evidence(question_id="q_does_not_exist")
        violations = check_evidence_grounding(
            corrupted, transcript=_real_transcript(), candidate_id="cand_real", job_id="job_real",
        )
        assert violations != []
        assert any("does not exist" in v for v in violations)

    def test_wrong_candidate_id_is_a_violation(self):
        corrupted = _real_evidence(candidate_id="cand_someone_else")
        violations = check_evidence_grounding(
            corrupted, transcript=_real_transcript(), candidate_id="cand_real", job_id="job_real",
        )
        assert violations != []
        assert any("candidate_id mismatch" in v for v in violations)

    def test_wrong_job_id_is_a_violation(self):
        wrong_job_transcript = _real_transcript().model_copy(update={"job_id": "job_other"})
        violations = check_evidence_grounding(
            _real_evidence(), transcript=wrong_job_transcript, candidate_id="cand_real", job_id="job_real",
        )
        assert violations != []
        assert any("job_id" in v for v in violations)

    def test_altered_evidence_text_is_a_violation(self):
        corrupted = _real_evidence(text="I single-handedly rebuilt the entire company's infrastructure.")
        violations = check_evidence_grounding(
            corrupted, transcript=_real_transcript(), candidate_id="cand_real", job_id="job_real",
        )
        assert violations != []
        assert any("does not match the real answer" in v for v in violations)

    def test_reference_to_nonexistent_answer_is_a_violation(self):
        """question_id points at a question that was asked, but the
        evidence claims to be an 'answer' with no real counterpart -
        modeled here as a question_id with no matching exchange at all,
        which is the concrete way this project represents "no such
        answer" (question and answer are always a paired exchange)."""
        no_such_exchange_transcript = InterviewTranscript(
            interview_id="int_2", candidate_id="cand_real", job_id="job_real",
            start_time="t0", is_sealed=True, exchanges=[],
        )
        violations = check_evidence_grounding(
            _real_evidence(), transcript=no_such_exchange_transcript, candidate_id="cand_real", job_id="job_real",
        )
        assert violations != []

    def test_empty_evidence_text_is_a_violation(self):
        """EvidenceItem itself enforces min_length=1, but a defensive check
        exists at the grounding layer too - verify it actually fires
        rather than assuming the schema constraint alone is enough."""
        real = _real_evidence()
        # Bypass the schema constraint via direct mutation (pydantic model,
        # not frozen) to prove the grounding sweep's OWN check, not just
        # pydantic's, catches this.
        real.text = "   "
        violations = check_evidence_grounding(
            real, transcript=_real_transcript(), candidate_id="cand_real", job_id="job_real",
        )
        assert any("empty evidence text" in v for v in violations)

    def test_multiple_corruptions_all_reported_not_just_the_first(self):
        corrupted = _real_evidence(question_id="q_missing", candidate_id="cand_wrong")
        violations = check_evidence_grounding(
            corrupted, transcript=_real_transcript(), candidate_id="cand_real", job_id="job_real",
        )
        assert len(violations) >= 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
