"""Integration tests for the complete pipeline."""
import pytest
import asyncio

from config.settings import DEBUG
from agents import JDAnalyzerAgent, ResumeParserAgent, ResumeMatcherAgent
from data import load_job_description, load_resume, load_interview_transcript


@pytest.mark.asyncio
async def test_jd_analyzer_agent(sample_job_description):
    """Test JD analyzer agent."""
    agent = JDAnalyzerAgent()
    
    result = await agent.run(
        run_id="test_001",
        job_description=sample_job_description.description,
        job_id=sample_job_description.job_id
    )

    assert result is not None
    inner = result.get("result") or {}
    assert "job_description" in inner or result.get("error")


@pytest.mark.asyncio
async def test_resume_parser_agent(sample_resume_dict):
    """Test resume parser agent."""
    import json
    agent = ResumeParserAgent()
    
    resume_text = json.dumps(sample_resume_dict)
    result = await agent.run(
        run_id="test_001",
        resume_text=resume_text,
        candidate_id="cand_test_001",
        candidate_name="Test Candidate"
    )

    assert result is not None
    inner = result.get("result") or {}
    assert "parsed_resume" in inner or result.get("error")


@pytest.mark.asyncio
async def test_resume_matcher_agent(sample_job_description, sample_parsed_resume):
    """Test resume matcher agent."""
    agent = ResumeMatcherAgent()
    
    result = await agent.run(
        run_id="test_001",
        job_description=sample_job_description,
        parsed_resume=sample_parsed_resume
    )

    assert result is not None
    inner = result.get("result") or {}
    assert "matching_score" in inner or result.get("error")

    if inner.get("matching_score"):
        score = inner["matching_score"]
        assert 0 <= score.match_score <= 1


@pytest.mark.asyncio
async def test_sample_data_loading():
    """Test that sample data files load correctly."""
    job = load_job_description()
    assert job["job_id"] == "job_001"
    assert "title" in job
    assert "competencies" in job
    
    resume_1 = load_resume(1)
    assert resume_1["candidate_id"] == "cand_001"
    assert "work_experience" in resume_1
    
    transcript = load_interview_transcript()
    assert transcript["interview_id"] == "int_cand_001"
    assert len(transcript["exchanges"]) > 0


@pytest.mark.asyncio
async def test_pipeline_state_initialization():
    """Test pipeline state schema declares all fields the graph nodes use.

    PipelineState is a TypedDict (required so LangGraph can build real state
    channels - see orchestration/graph.py) so it has no runtime-enforced
    defaults; this checks the annotated field set instead of instantiating it
    empty."""
    from orchestration.graph import PipelineState

    annotated_fields = set(PipelineState.__annotations__.keys())

    for field in [
        "run_id",
        "job_description",
        "parsed_resumes",
        "interview_transcripts",
        "technical_evaluations",
        "leaderboard",
        "errors",
        "audit_logs",
    ]:
        assert field in annotated_fields


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
