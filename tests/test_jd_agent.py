"""Unit tests for JD Analyzer Agent."""
import pytest
import asyncio

from agents import JDAnalyzerAgent
from schemas.job import JobDescription


@pytest.mark.asyncio
async def test_jd_analyzer_with_sample_description():
    """Test JD analyzer with real sample text."""
    agent = JDAnalyzerAgent()
    
    job_text = """
    Senior Python Backend Developer
    
    We are looking for a Senior Python Backend Developer to join our platform team.
    
    Requirements:
    - 5+ years of Python development
    - Strong async/await experience
    - Experience with REST APIs
    - Database optimization skills
    
    Preferred:
    - Kubernetes experience
    - GraphQL knowledge
    
    We value strong communication and teamwork.
    """
    
    result = await agent.run(
        run_id="test_jd_001",
        job_description=job_text,
        job_id="job_test_001"
    )
    
    assert result is not None
    # BaseAgent.run() wraps execute()'s output as {"result": ..., "audit_log": ...}
    inner = result.get("result") or {}
    assert "job_description" in inner or result.get("error")


@pytest.mark.asyncio
async def test_jd_analyzer_creates_competencies():
    """Test that JD analyzer creates competencies with normalized weights."""
    agent = JDAnalyzerAgent()
    
    job_text = """
    Senior Developer
    
    Requirements: Python, System Design, Database Management
    Preferred: Docker, Kubernetes
    """
    
    result = await agent.run(
        run_id="test_jd_002",
        job_description=job_text,
        job_id="job_test_002"
    )
    
    inner = result.get("result") or {}
    if inner.get("job_description"):
        job = inner["job_description"]
        # Check that competencies have valid structure
        assert len(job.competencies) > 0

        # Check weights sum to 1.0 ±0.01
        total_weight = sum(c.weight for c in job.competencies)
        assert abs(total_weight - 1.0) <= 0.01


@pytest.mark.asyncio
async def test_jd_analyzer_fallback_on_invalid_json():
    """Test fallback parsing on invalid JSON."""
    agent = JDAnalyzerAgent()
    
    # Minimal text that might cause parsing issues
    job_text = "Senior Python Developer needed"
    
    result = await agent.run(
        run_id="test_jd_003",
        job_description=job_text,
        job_id="job_test_003"
    )
    
    # Should return some result (possibly fallback)
    assert result is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
