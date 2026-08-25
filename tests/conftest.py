"""Pytest configuration and fixtures."""
import pytest
import json
from pathlib import Path

from schemas.job import JobDescription, Competency
from schemas.resume import ParsedResume, WorkExperience, Education
from schemas.interview import InterviewTranscript, InterviewQuestion, InterviewAnswer


@pytest.fixture
def sample_job_dict():
    """Sample job description."""
    return {
        "job_id": "job_test_001",
        "title": "Test Python Developer",
        "description": "Test job description",
        "experience_years": 3,
        "required_skills": ["Python", "REST APIs"],
        "preferred_skills": ["Docker"],
        "competencies": [
            {"name": "Python", "weight": 0.5},
            {"name": "System Design", "weight": 0.5},
        ]
    }


@pytest.fixture
def sample_job_description(sample_job_dict):
    """Sample JobDescription schema."""
    return JobDescription(
        job_id=sample_job_dict["job_id"],
        title=sample_job_dict["title"],
        description=sample_job_dict["description"],
        experience_years=sample_job_dict["experience_years"],
        required_skills=sample_job_dict["required_skills"],
        preferred_skills=sample_job_dict["preferred_skills"],
        competencies=[
            Competency(
                name=c["name"],
                weight=c["weight"],
                importance="high",
                description=""
            )
            for c in sample_job_dict["competencies"]
        ]
    )


@pytest.fixture
def sample_resume_dict():
    """Sample resume."""
    return {
        "candidate_id": "cand_test_001",
        "candidate_name": "Test Candidate",
        "email": "test@example.com",
        "phone": "+1-555-0000",
        "work_experience": [
            {
                "position": "Developer",
                "company": "Test Corp",
                "duration_years": 2,
                "duration_months": 6,
                "description": "Developed APIs",
                "achievements": ["Built REST API"],
            }
        ],
        "education": [
            {
                "degree": "BS",
                "field": "Computer Science",
                "institution": "Test University",
                "graduation_year": 2020,
            }
        ],
        "skills": ["Python", "REST APIs", "Docker"],
        "total_experience_years": 3,
    }


@pytest.fixture
def sample_parsed_resume(sample_resume_dict):
    """Sample ParsedResume schema."""
    from datetime import datetime

    current_year = datetime.now().year

    work_exp = [
        WorkExperience(
            position=w["position"],
            company=w["company"],
            start_year=current_year - int(w["duration_years"]),
            duration_months=w["duration_years"] * 12 + w["duration_months"],
            description=w["description"],
            achievements=w["achievements"],
        )
        for w in sample_resume_dict["work_experience"]
    ]

    education = [
        Education(
            degree=e["degree"],
            field_of_study=e["field"],
            institution=e["institution"],
            graduation_year=e["graduation_year"],
        )
        for e in sample_resume_dict["education"]
    ]
    
    return ParsedResume(
        candidate_id=sample_resume_dict["candidate_id"],
        candidate_name=sample_resume_dict["candidate_name"],
        email=sample_resume_dict["email"],
        phone=sample_resume_dict["phone"],
        work_experience=work_exp,
        education=education,
        projects=[],
        skills=sample_resume_dict["skills"],
        raw_text=json.dumps(sample_resume_dict),
        total_experience_years=sample_resume_dict["total_experience_years"],
    )


@pytest.fixture
def sample_interview_transcript():
    """A sealed InterviewTranscript with two real, distinct question/answer
    exchanges - used to test that evidence/job_id threading resolves to real
    transcript content rather than placeholders."""
    exchanges = [
        (
            InterviewQuestion(
                question_id="eq_tech_001",
                question_text="Tell us about a Python project you're proud of.",
                category="technical",
                difficulty="medium",
            ),
            InterviewAnswer(
                question_id="eq_tech_001",
                answer_text="I built an async FastAPI service that processes orders using asyncio.gather.",
                duration_seconds=40,
            ),
        ),
        (
            InterviewQuestion(
                question_id="eq_sql_002",
                question_text="How do you optimize a slow SQL query?",
                category="technical",
                difficulty="medium",
            ),
            InterviewAnswer(
                question_id="eq_sql_002",
                answer_text="I use EXPLAIN ANALYZE to find the bottleneck, then add targeted indexes.",
                duration_seconds=35,
            ),
        ),
    ]
    return InterviewTranscript(
        interview_id="int_test_001",
        candidate_id="cand_test_001",
        job_id="job_test_001",
        exchanges=exchanges,
        start_time="2024-01-15T10:00:00Z",
        is_sealed=True,
    )
