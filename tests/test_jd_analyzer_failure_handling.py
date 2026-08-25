"""Regression tests: a failed/invalid JD analysis must surface as an
explicit failure - JDAnalyzerAgent must never fabricate a plausible-looking
JobDescription (the removed _create_default_job_description() behavior),
and the graph must never let downstream candidate evaluation proceed on an
invented job description. Never makes a real API call."""
import json
import pytest

from agents.jd_analyzer.agent import JDAnalyzerAgent
from orchestration.graph import create_pipeline_graph, PipelineState
from tests.fakes import ScriptedLLMProvider

from data import load_all_resumes, load_interview_transcript
from main import build_resume_from_dict, build_transcript_from_dict


VALID_JD_JSON = json.dumps({
    "title": "Senior Python Developer",
    "required_skills": ["Python", "FastAPI"],
    "preferred_skills": ["Docker"],
    "experience_years": 5,
    "competencies": [
        {"name": "Python", "weight": 0.6},
        {"name": "System Design", "weight": 0.4},
    ],
    "interview_topics": ["Python fundamentals"],
})


def _empty_state(job_description_text: str, run_id: str = "run_test") -> PipelineState:
    resumes_dicts = load_all_resumes()
    parsed_resumes = [build_resume_from_dict(r) for r in resumes_dicts]
    base_transcript = build_transcript_from_dict(load_interview_transcript())

    interview_transcripts = {}
    for i in range(len(parsed_resumes)):
        candidate_id = f"cand_{i + 1:03d}"
        interview_transcripts[candidate_id] = base_transcript.model_copy(
            update={"candidate_id": candidate_id, "interview_id": f"int_{candidate_id}"}
        )

    return PipelineState(
        job_description_text=job_description_text,
        candidates_resume_texts=[r.raw_text for r in parsed_resumes],
        interview_transcripts=interview_transcripts,
        job_description=None,
        parsed_resumes={},
        matching_scores={},
        shortlisted_candidates=[],
        interview_questions={},
        technical_evaluations={},
        behavioral_evaluations={},
        resume_audits={},
        integrity_evaluations={},
        bias_audits={},
        candidate_scores={},
        candidate_reports={},
        leaderboard=None,
        run_id=run_id,
        errors=[],
        audit_logs=[],
    )


class TestJDAnalyzerAgentDirectly:
    """1-3: agent-level behavior for valid output, invalid structured
    output, and JobDescription construction failure."""

    @pytest.mark.asyncio
    async def test_valid_structured_jd_output_produces_normal_job_description(self):
        fake = ScriptedLLMProvider(script=[VALID_JD_JSON])
        agent = JDAnalyzerAgent(llm_provider=fake)

        result = await agent.execute(job_description="Some job text", job_id="job_001")

        assert result.get("error") is None
        job_desc = result["job_description"]
        assert job_desc is not None
        assert job_desc.title == "Senior Python Developer"
        assert len(job_desc.competencies) == 2

    @pytest.mark.asyncio
    async def test_invalid_structured_output_is_explicit_failure_not_fabricated_jd(self):
        """The LLM returns JSON that fails JDAnalysisResult schema validation
        (experience_years must be an int, not a string) - repeated failure
        must surface as an explicit error with job_description=None, never a
        fabricated 'Unstructured Job' placeholder."""
        bad_json = json.dumps({"title": "Some Role", "experience_years": "not-a-number"})
        fake = ScriptedLLMProvider(script=[bad_json])
        agent = JDAnalyzerAgent(llm_provider=fake)

        result = await agent.execute(job_description="Some job text", job_id="job_001")

        assert result["job_description"] is None
        assert result.get("error") is not None
        assert "Unstructured Job" not in str(result)

    @pytest.mark.asyncio
    async def test_jobdescription_construction_failure_is_explicit_failure(self):
        """Structurally-valid JDAnalysisResult, but the resulting
        JobDescription itself fails validation (competency weight out of
        [0,1] range is rejected by schemas.job.Competency even after the
        agent's own normalization step, e.g. because normalization only
        fixes the SUM, not a NaN/negative value) - must still surface as an
        explicit failure, not a fabricated default."""
        # A negative weight survives sum-based normalization (sum could
        # still net to ~1.0 with a negative offset) but is rejected by
        # Competency.weight's ge=0.0 constraint when JobDescription is built.
        weird_json = json.dumps({
            "title": "Some Role",
            "competencies": [
                {"name": "A", "weight": 1.5},
                {"name": "B", "weight": -0.5},
            ],
        })
        fake = ScriptedLLMProvider(script=[weird_json])
        agent = JDAnalyzerAgent(llm_provider=fake)

        result = await agent.execute(job_description="Some job text", job_id="job_001")

        assert result["job_description"] is None
        assert result.get("error") is not None

    @pytest.mark.asyncio
    async def test_no_default_job_description_method_exists(self):
        """The fabricated-fallback method itself is gone, not just unused."""
        assert not hasattr(JDAnalyzerAgent, "_create_default_job_description")


class TestGraphNeverProceedsOnFabricatedJD:
    """4-5: the graph node must not treat a failed analysis as successful,
    and downstream candidate evaluation must not run against an invented JD."""

    @pytest.mark.asyncio
    async def test_failed_jd_analysis_leaves_job_description_unset_and_errors_explicit(self, monkeypatch):
        async def failing_execute(self, job_description, job_id="job_001", **kwargs):
            return {"job_description": None, "error": "simulated JD analysis failure"}

        monkeypatch.setattr(JDAnalyzerAgent, "execute", failing_execute)

        state = _empty_state(job_description_text="Some job posting text")
        pipeline = create_pipeline_graph()
        result = await pipeline.ainvoke(state)

        assert result["job_description"] is None
        assert any("JD Analysis failed" in e and "simulated JD analysis failure" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_no_candidates_are_evaluated_or_scored_when_jd_analysis_fails(self, monkeypatch):
        """Downstream candidate evaluation must not run using an invented
        JD - with job_description=None, nobody gets shortlisted, so nobody
        reaches matching/questions/evaluation/scoring."""
        async def failing_execute(self, job_description, job_id="job_001", **kwargs):
            return {"job_description": None, "error": "simulated JD analysis failure"}

        monkeypatch.setattr(JDAnalyzerAgent, "execute", failing_execute)

        state = _empty_state(job_description_text="Some job posting text")
        pipeline = create_pipeline_graph()
        result = await pipeline.ainvoke(state)

        assert result["shortlisted_candidates"] == []
        assert result["matching_scores"] == {}
        assert result["technical_evaluations"] == {}
        assert result["behavioral_evaluations"] == {}
        assert result["candidate_scores"] == {}
        assert result["candidate_reports"] == {}
        # No leaderboard is fabricated either - nothing was ever shortlisted.
        assert result.get("leaderboard") is None
        assert any("nothing to rank" in e.lower() or "no shortlisted candidates" in e.lower()
                   for e in result["errors"])


class TestMockPipelineStillSucceeds:
    """6: with the real (working) mock provider, the pipeline still
    completes normally end-to-end - this fix only changes the failure path."""

    @pytest.mark.asyncio
    async def test_full_mock_pipeline_still_succeeds(self):
        state = _empty_state(
            job_description_text="Senior Python Backend Developer role requiring async experience.",
            run_id="run_test_success",
        )
        pipeline = create_pipeline_graph()
        result = await pipeline.ainvoke(state)

        assert result["job_description"] is not None
        assert result["errors"] == []
        assert result["leaderboard"] is not None
        assert len(result["candidate_scores"]) == 3
        assert len(result["candidate_reports"]) == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
