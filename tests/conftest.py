"""Pytest configuration and fixtures.

Provider isolation (read this before changing the block below)
--------------------------------------------------------------
The test suite is a MOCK-mode suite: every test either uses the
deterministic MockLLMProvider/MockAudioProcessor defaults or injects its own
fake through an explicit seam (tests/fakes.py, `app.state.interviewer_factory`,
`app.state.voice_service_factory`). No test is intended to reach a real
provider, and one of them - test_api.py::test_default_config_has_no_api_key_required
- asserts exactly that by checking `LLM_PROVIDER == "mock"`.

`config/settings.py` reads those provider choices from the process
environment at import time, and `load_dotenv()` means a developer's local
`.env` is part of that environment. So running the suite on a machine
configured for real Groq or real Azure Speech silently redirected ten tests
at live, rate-limited, paid APIs - which is how they were observed failing
with HTTP 429 rather than with a code defect.

Pinning them here, before any project module is imported, makes the suite
hermetic: its result now depends only on the code under test. `setdefault`
is used, not assignment, so an explicitly exported environment variable
still wins - a deliberate real-provider run (`LLM_PROVIDER=groq pytest ...`)
is unaffected.

DATABASE_URL joined this list for the exact same reason (Database chunk):
a developer's `.env` may set it so a manually-run server uses PostgreSQL
(core/container.py:build_default_container), but a test that builds its app
via `get_settings()`/`AppSettings.from_env()` rather than an explicit
`AppSettings()` (most of the FastAPI `TestClient` fixtures do) would then
silently get a REAL `PostgresConnectionPool` instead of the in-memory
stubs the suite is written against - and since pytest-asyncio gives most
tests their own event loop, that pool ends up reused across loops the same
way `tests/test_postgres_repositories.py` had to guard against, producing
"Event loop is closed" failures in tests that were never meant to touch a
database at all. Pinning it empty here keeps `DATABASE_URL` fully separate
from `TEST_DATABASE_URL` (which `tests/test_postgres_repositories.py`
reads on purpose to opt into a real Postgres run) - this suite's default
behaviour never depends on what a developer's `.env` happens to contain.
"""
import os

# Must run before any import that pulls in config.settings.
for _var, _value in (
    ("LLM_PROVIDER", "mock"),
    ("AUDIO_PROVIDER", "mock"),
    ("AUDIO_PROCESSOR", "mock"),
    ("TTS_PROVIDER", "mock"),
    ("VECTOR_STORE_TYPE", "mock"),
    ("DATABASE_URL", ""),
    ("ENVIRONMENT", "test"),
    ("DATABASE_URL", ""),
):
    os.environ.setdefault(_var, _value)

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
