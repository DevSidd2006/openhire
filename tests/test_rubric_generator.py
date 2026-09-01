"""Tests for rubric drafting from a job description (Task 7).

The draft is never auto-approved: a rubric decides every ranking on its job,
so an unreviewed one is a loophole in its own right.
"""
import pytest

from agents.rubric_generator.agent import RubricGeneratorAgent
from schemas.job import JobDescription
from schemas.rubric import RubricStatus


class _StubLLM:
    def __init__(self, payload):
        self.payload = payload

    async def generate_structured(self, prompt, schema, **kwargs):
        return self.payload


def _job():
    return JobDescription(
        job_id="job_1",
        title="Backend Engineer",
        description="Build and run Python services on Kubernetes.",
        required_skills=["Python", "Kubernetes"],
        experience_years=3,
    )


def _payload():
    anchors = {"1": "none", "2": "emerging", "3": "proficient", "4": "strong", "5": "exceptional"}
    return {
        "competencies": [
            {"name": "Python engineering", "definition": "Writes production Python",
             "weight": 0.4, "anchors": anchors},
            {"name": "Kubernetes operations", "definition": "Runs clusters",
             "weight": 0.35, "anchors": anchors},
            {"name": "Ownership", "definition": "Drives work to completion",
             "weight": 0.25, "anchors": anchors},
        ]
    }


@pytest.mark.asyncio
async def test_generates_a_draft_rubric():
    agent = RubricGeneratorAgent(llm_provider=_StubLLM(_payload()))
    result = await agent.execute(job_description=_job())
    rubric = result["rubric"]
    assert rubric.status is RubricStatus.DRAFT
    assert len(rubric.competencies) == 3


@pytest.mark.asyncio
async def test_generated_rubric_passes_its_own_approval_gate():
    agent = RubricGeneratorAgent(llm_provider=_StubLLM(_payload()))
    result = await agent.execute(job_description=_job())
    assert result["rubric"].validate_approvable() == []


@pytest.mark.asyncio
async def test_rubric_is_never_auto_approved():
    agent = RubricGeneratorAgent(llm_provider=_StubLLM(_payload()))
    result = await agent.execute(job_description=_job())
    assert result["rubric"].status is not RubricStatus.APPROVED


@pytest.mark.asyncio
async def test_weights_are_normalized_when_the_model_returns_a_bad_sum():
    payload = _payload()
    payload["competencies"][0]["weight"] = 0.9  # now sums to 1.5
    agent = RubricGeneratorAgent(llm_provider=_StubLLM(payload))
    result = await agent.execute(job_description=_job())
    assert result["rubric"].validate_approvable() == []


@pytest.mark.asyncio
async def test_normalization_preserves_relative_emphasis():
    """Rescaling must not reorder the model's intended priorities."""
    payload = _payload()
    payload["competencies"][0]["weight"] = 0.9
    agent = RubricGeneratorAgent(llm_provider=_StubLLM(payload))
    result = await agent.execute(job_description=_job())
    weights = {c.name: c.weight for c in result["rubric"].competencies}
    assert weights["Python engineering"] > weights["Kubernetes operations"] > weights["Ownership"]


@pytest.mark.asyncio
async def test_rubric_version_starts_at_one():
    agent = RubricGeneratorAgent(llm_provider=_StubLLM(_payload()))
    result = await agent.execute(job_description=_job())
    assert result["rubric"].version == 1
