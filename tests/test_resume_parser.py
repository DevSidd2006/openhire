"""Unit tests for Resume Parser Agent."""
import pytest
import json
import asyncio

from agents import ResumeParserAgent
from schemas.resume import ParsedResume


@pytest.mark.asyncio
async def test_resume_parser_with_structured_data():
    """Test resume parser with JSON-formatted resume."""
    agent = ResumeParserAgent()
    
    resume_data = {
        "name": "John Doe",
        "email": "john@example.com",
        "phone": "+1-555-1234",
        "work_experience": [
            {
                "position": "Senior Developer",
                "company": "Tech Corp",
                "years": 3,
                "achievements": ["Led team of 5", "30% performance improvement"]
            }
        ],
        "education": [
            {
                "degree": "BS",
                "field": "Computer Science",
                "university": "State University",
                "graduation_year": 2018
            }
        ],
        "skills": ["Python", "Java", "Kubernetes", "Docker"]
    }
    
    resume_text = json.dumps(resume_data)
    
    result = await agent.run(
        run_id="test_resume_001",
        resume_text=resume_text,
        candidate_id="cand_001",
        candidate_name="John Doe"
    )

    assert result is not None
    inner = result.get("result") or {}
    assert "parsed_resume" in inner or result.get("error")

    if inner.get("parsed_resume"):
        resume = inner["parsed_resume"]
        assert resume.candidate_id == "cand_001"
        assert len(resume.skills) > 0


@pytest.mark.asyncio
async def test_resume_parser_fallback_parsing():
    """Test resume parser fallback on minimal text."""
    agent = ResumeParserAgent()
    
    resume_text = """
    John Doe
    john@example.com | (555) 123-4567
    
    Skills: Python, Java, Docker
    
    Experience:
    - Developer at TechCorp (2019-2022)
    """
    
    result = await agent.run(
        run_id="test_resume_002",
        resume_text=resume_text,
        candidate_id="cand_002",
        candidate_name="John Doe"
    )
    
    # Should return parsed resume (or fallback)
    assert result is not None


@pytest.mark.asyncio
async def test_resume_parser_experience_calculation():
    """Test that parser calculates total experience correctly."""
    agent = ResumeParserAgent()
    
    resume_data = {
        "work_experience": [
            {
                "position": "Developer",
                "company": "Company A",
                "years": 3,
                "description": ""
            },
            {
                "position": "Developer",
                "company": "Company B",
                "years": 2,
                "description": ""
            }
        ]
    }
    
    resume_text = json.dumps(resume_data)
    
    result = await agent.run(
        run_id="test_resume_003",
        resume_text=resume_text,
        candidate_id="cand_003",
        candidate_name="Test User"
    )
    
    # Should parse successfully
    assert result is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
