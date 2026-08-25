"""P1 agent tests: verify each migrated agent actually calls
LLMProvider.generate_structured() - not the old generate() + manual
json.loads() path - and that the final agent output is still correct. Uses
SpyLLMProvider wrapping a real MockLLMProvider, so responses are the actual
deterministic mock data, not hand-faked."""
import pytest

from agents.jd_analyzer.agent import JDAnalyzerAgent
from agents.technical_evaluator.agent import TechnicalEvaluatorAgent
from agents.behavioral_evaluator.agent import BehavioralEvaluatorAgent
from agents.resume_auditor.agent import ResumeAuditorAgent
from agents.integrity.agent import IntegrityAgent
from agents.bias_checker.agent import BiasCheckerAgent
from agents.interviewer.agent import InterviewerAgent
from providers.llm.mock import MockLLMProvider
from tests.fakes import SpyLLMProvider


def _spy():
    return SpyLLMProvider(MockLLMProvider())


class TestJDAnalyzerUsesStructuredOutput:
    @pytest.mark.asyncio
    async def test_calls_generate_structured_not_generate(self):
        spy = _spy()
        agent = JDAnalyzerAgent(llm_provider=spy)

        result = await agent.execute(job_description="Senior Python Developer role.", job_id="job_001")

        assert len(spy.generate_structured_calls) == 1
        assert spy.generate_calls == []
        assert result["job_description"] is not None
        assert result["job_description"].job_id == "job_001"


class TestTechnicalEvaluatorUsesStructuredOutput:
    @pytest.mark.asyncio
    async def test_calls_generate_structured_not_generate(
        self, sample_job_description, sample_parsed_resume, sample_interview_transcript
    ):
        spy = _spy()
        agent = TechnicalEvaluatorAgent(llm_provider=spy)

        result = await agent.execute(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )

        assert len(spy.generate_structured_calls) == 1
        assert spy.generate_calls == []
        assert result["technical_evaluation"] is not None


class TestBehavioralEvaluatorUsesStructuredOutput:
    @pytest.mark.asyncio
    async def test_calls_generate_structured_not_generate(
        self, sample_job_description, sample_parsed_resume, sample_interview_transcript
    ):
        spy = _spy()
        agent = BehavioralEvaluatorAgent(llm_provider=spy)

        result = await agent.execute(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )

        assert len(spy.generate_structured_calls) == 1
        assert spy.generate_calls == []
        assert result["behavioral_evaluation"] is not None


class TestResumeAuditorUsesStructuredOutput:
    @pytest.mark.asyncio
    async def test_calls_generate_structured_not_generate(
        self, sample_parsed_resume, sample_interview_transcript
    ):
        spy = _spy()
        agent = ResumeAuditorAgent(llm_provider=spy)

        result = await agent.execute(
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )

        # One structured call per extracted claim, none of the old unstructured path.
        assert len(spy.generate_structured_calls) == len(result["claim_verifications"])
        assert len(spy.generate_structured_calls) >= 1
        assert spy.generate_calls == []


class TestIntegrityAgentUsesStructuredOutput:
    @pytest.mark.asyncio
    async def test_calls_generate_structured_not_generate(
        self, sample_parsed_resume, sample_interview_transcript
    ):
        spy = _spy()
        agent = IntegrityAgent(llm_provider=spy)

        result = await agent.execute(
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )

        assert len(spy.generate_structured_calls) == 1
        assert spy.generate_calls == []
        assert result["integrity_evaluation"] is not None


class TestBiasCheckerUsesStructuredOutput:
    @pytest.mark.asyncio
    async def test_calls_generate_structured_not_generate(self, sample_interview_transcript):
        spy = _spy()
        agent = BiasCheckerAgent(llm_provider=spy)

        result = await agent.execute(
            interview_transcript=sample_interview_transcript,
            candidate_id="cand_test_001",
            job_id="job_test_001",
            evaluation_sections={"technical_evaluation": "Strong async knowledge demonstrated."},
        )

        assert len(spy.generate_structured_calls) == 1
        assert spy.generate_calls == []
        assert result["bias_audit"] is not None


class TestInterviewerUsesStructuredOutput:
    @pytest.mark.asyncio
    async def test_calls_generate_structured_not_generate(
        self, sample_job_description, sample_parsed_resume
    ):
        spy = _spy()
        agent = InterviewerAgent(llm_provider=spy)

        result = await agent.execute(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            question_count=2,
        )

        assert len(spy.generate_structured_calls) == 2
        assert spy.generate_calls == []
        assert len(result["questions"]) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
