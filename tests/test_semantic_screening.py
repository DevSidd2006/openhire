"""Tests for rubric-free semantic screening (resume-to-JD similarity).

The contract under test is narrow but load-bearing: this score must exist
even when a job has no approved rubric (that is the only reason it exists),
and an embedding outage must produce None rather than 0.0, so infrastructure
trouble never renders as a bottom-ranked candidate.
"""
import pytest

from schemas.application import ApplicationStatus
from schemas.job import JobDescription
from schemas.resume import Education, ParsedResume, WorkExperience
from services.semantic_matching import EmbeddingUnavailableError
from services.semantic_screening import (
    SemanticScreeningService,
    render_job_text,
    render_resume_text,
)


class _StubMatcher:
    """Stands in for SemanticMatcher; records what it was asked to compare."""

    def __init__(self, *, similarity=0.83, fail=False):
        self.similarity = similarity
        self.fail = fail
        self.calls = []

    async def calculate_job_description_similarity(self, resume_text, job_text):
        self.calls.append((resume_text, job_text))
        if self.fail:
            raise EmbeddingUnavailableError("provider down")
        return self.similarity


def _job() -> JobDescription:
    return JobDescription(
        job_id="job_1",
        title="Software Development Engineer",
        description="Design and deploy scalable APIs and backend systems.",
        required_skills=["Python", "PostgreSQL", "Docker"],
        responsibilities=["Code review", "System design"],
    )


def _resume() -> ParsedResume:
    return ParsedResume(
        candidate_id="cand_1",
        candidate_name="Arjun Mehta",
        summary="Backend engineer building scalable APIs.",
        skills=["Python", "PostgreSQL", "Docker"],
        work_experience=[
            WorkExperience(
                company="Payments Co", position="SDE II", start_year=2023,
                is_current=True, achievements=["Cut p99 latency from 820ms to 180ms"],
            )
        ],
        education=[
            Education(institution="NIT", degree="B.Tech", field_of_study="CS")
        ],
    )


@pytest.mark.asyncio
async def test_score_returns_similarity_without_any_rubric():
    matcher = _StubMatcher(similarity=0.77)
    service = SemanticScreeningService(semantic_matcher=matcher)

    score = await service.score(_job(), _resume())

    assert score == 0.77
    # No rubric was passed or consulted anywhere in this path.
    resume_text, job_text = matcher.calls[0]
    assert "PostgreSQL" in resume_text
    assert "Software Development Engineer" in job_text


@pytest.mark.asyncio
async def test_embedding_outage_yields_none_not_zero():
    """A 0.0 here would be indistinguishable from a terrible candidate."""
    service = SemanticScreeningService(semantic_matcher=_StubMatcher(fail=True))

    assert await service.score(_job(), _resume()) is None


@pytest.mark.asyncio
async def test_empty_resume_is_not_scored():
    empty = ParsedResume(candidate_id="cand_2", candidate_name="Nobody")
    matcher = _StubMatcher()
    service = SemanticScreeningService(semantic_matcher=matcher)

    assert await service.score(_job(), empty) is None
    assert matcher.calls == []


def test_render_resume_text_uses_citable_spans():
    text = render_resume_text(_resume())

    assert "Backend engineer building scalable APIs." in text
    assert "Cut p99 latency from 820ms to 180ms" in text
    assert "B.Tech in CS, NIT" in text


def test_render_job_text_includes_skills_and_responsibilities():
    text = render_job_text(_job())

    assert "Required skills: Python, PostgreSQL, Docker" in text
    assert "Responsibilities: Code review; System design" in text


class _ExplodingMatcher:
    """Stands in for a provider that cannot even be constructed.

    `providers/embeddings/__init__.py` raises a plain ValueError when the
    configured provider has no API key - the normal state of a deployment
    that has never used embeddings.
    """

    async def calculate_job_description_similarity(self, resume_text, job_text):
        raise ValueError("GEMINI_API_KEY environment variable is required")


@pytest.mark.asyncio
async def test_unconfigured_embedding_provider_does_not_propagate():
    """A missing optional API key must not 500 the whole matching run.

    Rubric scoring does not depend on embeddings, so an embedding
    misconfiguration must degrade this one column and nothing else.
    """
    service = SemanticScreeningService(semantic_matcher=_ExplodingMatcher())

    assert await service.score(_job(), _resume()) is None
