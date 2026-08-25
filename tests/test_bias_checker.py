"""Tests for P0-5: the bias checker must actually receive evaluator
rationale (not just the raw transcript), and must ground any evidence it
cites in that real rationale text rather than fabricating it."""
import json
import pytest

from agents.bias_checker.agent import BiasCheckerAgent
from orchestration.graph import _build_evaluation_sections
from schemas.evaluation import TechnicalEvaluation, BehavioralEvaluation
from tests.fakes import SingleResponseLLMProvider


def _tech_eval():
    return TechnicalEvaluation(
        evaluation_id="tech_1",
        candidate_id="cand_test_001",
        job_id="job_test_001",
        interview_id="int_test_001",
        technical_score=8.0,
        strengths=["Strong async knowledge"],
        weaknesses=["Limited SQL depth"],
        explanation="Candidate demonstrated deep FastAPI/asyncio experience.",
        confidence=0.8,
    )


def _behav_eval():
    return BehavioralEvaluation(
        evaluation_id="behav_1",
        candidate_id="cand_test_001",
        job_id="job_test_001",
        interview_id="int_test_001",
        behavioral_score=7.0,
        communication=7.0,
        problem_solving=7.0,
        teamwork=7.0,
        adaptability=7.0,
        strengths=["Clear communicator"],
        weaknesses=[],
        explanation="Candidate was articulate and collaborative.",
        confidence=0.75,
    )


class TestBuildEvaluationSections:
    def test_sections_contain_real_evaluator_text(self):
        sections = _build_evaluation_sections(_tech_eval(), _behav_eval(), None, None)
        assert "FastAPI/asyncio" in sections["technical_evaluation"]
        assert "articulate and collaborative" in sections["behavioral_evaluation"]
        assert "resume_audit" not in sections
        assert "integrity" not in sections


class TestBiasCheckerReceivesRationale:
    @pytest.mark.asyncio
    async def test_evaluation_rationale_reaches_the_prompt(self, sample_interview_transcript):
        """The orchestration bug (P0-5) was that evaluation_text never
        reached the bias checker at all. Assert the actual evaluator
        rationale text is present in the prompt the LLM sees."""
        fake = SingleResponseLLMProvider(json.dumps({
            "flags": [],
            "fairness_status": "pass",
            "explanation": "No bias detected",
            "confidence": 0.8,
        }))
        agent = BiasCheckerAgent(llm_provider=fake)

        sections = _build_evaluation_sections(_tech_eval(), _behav_eval(), None, None)
        await agent.execute(
            interview_transcript=sample_interview_transcript,
            candidate_id="cand_test_001",
            job_id="job_test_001",
            technical_score=8.0,
            behavioral_score=7.0,
            evaluation_sections=sections,
        )

        assert len(fake.calls) == 1
        prompt = fake.calls[0]
        assert "FastAPI/asyncio" in prompt
        assert "articulate and collaborative" in prompt

    @pytest.mark.asyncio
    async def test_flag_evidence_grounded_in_real_rationale_text(self, sample_interview_transcript):
        fake = SingleResponseLLMProvider(json.dumps({
            "flags": [
                {
                    "bias_type": "personality_assumption",
                    "severity": "low",
                    "confidence": 0.6,
                    "evidence_source": "behavioral_evaluation",
                    "description": "Praise focuses on personality trait, not a demonstrated skill",
                    "recommendation": "Re-anchor to job-relevant behaviors",
                }
            ],
            "fairness_status": "flagged",
            "explanation": "One concern found",
            "confidence": 0.7,
        }))
        agent = BiasCheckerAgent(llm_provider=fake)
        behav_eval = _behav_eval()
        sections = _build_evaluation_sections(_tech_eval(), behav_eval, None, None)

        result = await agent.execute(
            interview_transcript=sample_interview_transcript,
            candidate_id="cand_test_001",
            job_id="job_test_001",
            technical_score=8.0,
            behavioral_score=7.0,
            evaluation_sections=sections,
        )
        audit = result["bias_audit"]
        assert len(audit.flags) == 1
        evidence = audit.flags[0].evidence
        assert len(evidence) == 1
        # Evidence text must be the REAL section text, not something invented.
        assert evidence[0].text == sections["behavioral_evaluation"]
        assert evidence[0].source_id == "behavioral_evaluation"

    @pytest.mark.asyncio
    async def test_flag_with_unresolvable_source_keeps_flag_without_fabricated_evidence(
        self, sample_interview_transcript
    ):
        fake = SingleResponseLLMProvider(json.dumps({
            "flags": [
                {
                    "bias_type": "other",
                    "severity": "low",
                    "confidence": 0.5,
                    "evidence_source": "resume_audit",  # not provided this run
                    "description": "Unclear",
                    "recommendation": "Review manually",
                }
            ],
            "fairness_status": "flagged",
            "explanation": "N/A",
            "confidence": 0.5,
        }))
        agent = BiasCheckerAgent(llm_provider=fake)
        sections = _build_evaluation_sections(_tech_eval(), _behav_eval(), None, None)

        result = await agent.execute(
            interview_transcript=sample_interview_transcript,
            candidate_id="cand_test_001",
            job_id="job_test_001",
            evaluation_sections=sections,
        )
        audit = result["bias_audit"]
        assert len(audit.flags) == 1
        assert audit.flags[0].evidence == []

    @pytest.mark.asyncio
    async def test_no_bias_is_a_valid_empty_result(self, sample_interview_transcript):
        fake = SingleResponseLLMProvider(json.dumps({
            "flags": [],
            "fairness_status": "pass",
            "explanation": "Evaluation appears fair and job-relevant",
            "confidence": 0.8,
        }))
        agent = BiasCheckerAgent(llm_provider=fake)
        sections = _build_evaluation_sections(_tech_eval(), _behav_eval(), None, None)

        result = await agent.execute(
            interview_transcript=sample_interview_transcript,
            candidate_id="cand_test_001",
            job_id="job_test_001",
            evaluation_sections=sections,
        )
        assert result["bias_audit"].flags == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
