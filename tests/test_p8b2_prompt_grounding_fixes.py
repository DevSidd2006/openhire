"""P8B.2: regression tests for the two prompt-construction fixes found by
the expanded real-Gemini pilot.

Both fixes are prompt-text-only changes (plus, for the evaluators, one new
prompt variable) - they cannot affect ScriptedLLMProvider-driven mock
evaluation at all (the mock path never reads prompt content), so these
tests assert on the actual constructed prompt text, not on scored output.

1. TechnicalEvaluatorAgent / BehavioralEvaluatorAgent: the prompt now
   explicitly lists the job's exact competency names, so a real LLM has an
   authoritative source for the JSON keys `competency_scores` must use -
   previously the prompt asked the LLM to score "each technical competency
   in the rubric" without ever transmitting what the rubric's competency
   names actually were (the pilot's `tech_strong_answer_grounded` failure).
   The exact-string lookup in both agents (`result.competency_scores.get(
   comp.name)`) is intentionally left unchanged here (not fuzzy-matched) -
   see the P8B.2 final report for why that stays a documented, undecided
   question rather than a speculative fix.

2. JDAnalyzerAgent: the prompt no longer pressures the LLM to always invent
   "at least 4 competencies" regardless of how little source material a
   sparse job description provides, and now says explicitly not to
   fabricate skills/competencies beyond what the text supports (previously
   the only migrated-agent prompt with zero anti-fabrication guidance).
"""
import pytest

from agents.behavioral_evaluator.agent import BehavioralEvaluatorAgent
from agents.jd_analyzer.agent import JDAnalyzerAgent
from agents.technical_evaluator.agent import TechnicalEvaluatorAgent
from providers.llm.mock import MockLLMProvider
from tests.fakes import SpyLLMProvider


def _spy():
    return SpyLLMProvider(MockLLMProvider())


class TestTechnicalEvaluatorCompetencyListInPrompt:
    @pytest.mark.asyncio
    async def test_prompt_contains_exact_competency_names(
        self, sample_job_description, sample_parsed_resume, sample_interview_transcript
    ):
        spy = _spy()
        agent = TechnicalEvaluatorAgent(llm_provider=spy)

        await agent.execute(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )

        assert len(spy.generate_structured_calls) == 1
        prompt = spy.generate_structured_calls[0]
        assert "- Python" in prompt
        assert "- System Design" in prompt
        assert "EXACT strings" in prompt

    @pytest.mark.asyncio
    async def test_no_competencies_renders_explicit_placeholder_not_empty(
        self, sample_parsed_resume, sample_interview_transcript
    ):
        from schemas.job import JobDescription

        job = JobDescription(job_id="job_none", title="T", description="d", competencies=[])
        spy = _spy()
        agent = TechnicalEvaluatorAgent(llm_provider=spy)

        await agent.execute(
            job_description=job,
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )

        assert "(none specified)" in spy.generate_structured_calls[0]


class TestBehavioralEvaluatorCompetencyListInPrompt:
    @pytest.mark.asyncio
    async def test_prompt_contains_exact_competency_names(
        self, sample_job_description, sample_parsed_resume, sample_interview_transcript
    ):
        spy = _spy()
        agent = BehavioralEvaluatorAgent(llm_provider=spy)

        await agent.execute(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )

        assert len(spy.generate_structured_calls) == 1
        prompt = spy.generate_structured_calls[0]
        assert "- Python" in prompt
        assert "- System Design" in prompt
        assert "EXACT strings" in prompt


class TestCompetencyLookupStaysExactMatch:
    """Locks in the CURRENT, deliberately-unchanged behavior: a competency
    name the LLM reports that does not exactly match the job's canonical
    name still resolves to no judgment for that competency, rather than
    being fuzzy-matched. The P8B.2 pilot could not confirm whether the
    observed real-Gemini failure was caused by a naming mismatch or by the
    LLM omitting the competency entirely (see report); until that is
    confirmed, silently fuzzy-matching competency names would be a
    speculative fix, not a demonstrated one - this test documents and
    protects the current, safer default."""

    @pytest.mark.asyncio
    async def test_technical_evaluator_ignores_non_matching_competency_key(
        self, sample_parsed_resume, sample_interview_transcript
    ):
        import json
        from tests.fakes import ScriptedLLMProvider

        script = [json.dumps({
            "technical_score": 8.0,
            "competency_scores": {
                "Python Programming": {  # not the canonical "Python"
                    "score": 8.0, "confidence": 0.8,
                    "evidence_question_number": 1, "explanation": "x",
                },
            },
            "strengths": [], "weaknesses": [], "explanation": "x", "confidence": 0.8,
        })]
        from schemas.job import JobDescription
        from schemas.job import Competency

        job = JobDescription(
            job_id="job_x", title="T", description="d",
            competencies=[Competency(name="Python", weight=1.0)],
        )
        agent = TechnicalEvaluatorAgent(llm_provider=ScriptedLLMProvider(script=script))

        result = await agent.execute(
            job_description=job,
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )

        assert result["technical_evaluation"].competency_scores == []


class TestJDAnalyzerPromptNoLongerForcesMinimumCompetencyCount:
    @pytest.mark.asyncio
    async def test_prompt_does_not_mandate_at_least_4_competencies(self):
        spy = _spy()
        agent = JDAnalyzerAgent(llm_provider=spy)

        await agent.execute(job_description="We need a developer.", job_id="job_sparse")

        prompt = spy.generate_structured_calls[0]
        assert "At least 4 competencies" not in prompt
        assert "do NOT fabricate" in prompt.lower() or "do not fabricate" in prompt.lower()

    @pytest.mark.asyncio
    async def test_prompt_tells_llm_empty_lists_are_valid_for_sparse_input(self):
        spy = _spy()
        agent = JDAnalyzerAgent(llm_provider=spy)

        await agent.execute(job_description="We need a developer.", job_id="job_sparse")

        prompt = spy.generate_structured_calls[0].lower()
        assert "empty list is a valid" in prompt


class TestBiasCheckerPromptTransmitsExactBiasTypeVocabulary:
    """P8B (first incremental Groq benchmark batch): bias_checker scored
    5/7 against real Groq, and BOTH failures were the identical defect -
    the model DETECTED the bias correctly (right count, right evidence,
    right severity) but named `bias_type` "age_bias" instead of "age" and
    "appearance_based_judgments" instead of "appearance".

    Root cause: prompts/bias_checker.md listed its audit categories only as
    English prose headings ("Demographic-based reasoning", "Appearance-based
    judgments") and gave exactly ONE example value in the output template,
    while `BiasFlagResult.bias_type` is an unconstrained `str` and every
    downstream consumer (evaluation/cases/bias_checker.json, reporting)
    matches it EXACTLY. The model was therefore never told which strings
    were legal and reasonably derived labels from the prose headings.

    This is the same class of defect P8B.2 fixed for competency names, and
    the fix is the same: transmit the exact allowed vocabulary. NOTE this
    did NOT weaken any expectation - the golden cases still require exactly
    "age" and "appearance"; only the prompt changed, so the model is now
    told the contract it was always being held to."""

    @pytest.mark.asyncio
    async def test_prompt_enumerates_the_exact_allowed_bias_type_values(self):
        from agents.bias_checker.agent import BiasCheckerAgent
        from schemas.interview import InterviewTranscript

        spy = _spy()
        agent = BiasCheckerAgent(llm_provider=spy)
        transcript = InterviewTranscript(
            interview_id="int_vocab", candidate_id="cand_vocab", job_id="job_vocab",
            start_time="t0", is_sealed=True, exchanges=[],
        )

        await agent.execute(
            interview_transcript=transcript, candidate_id="cand_vocab", job_id="job_vocab",
            technical_score=7.0, behavioral_score=7.0,
            evaluation_sections={"technical_evaluation": "Impressive depth for someone so young."},
        )

        prompt = spy.generate_structured_calls[0]
        # Every value the golden dataset and schema actually use must be
        # transmitted, or the model cannot be expected to produce them.
        for allowed in ("demographic", "age", "appearance", "accent", "personality_assumption", "other"):
            assert f"`{allowed}`" in prompt, f"bias_type value {allowed!r} not transmitted to the model"
        # And the two variants real Groq actually invented are called out.
        assert "age_bias" in prompt
        assert "appearance_based_judgments" in prompt


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
