"""Tests for versioned rubric storage (Task 10).

The invariant that matters: at most one APPROVED rubric per job. A
leaderboard whose rows were scored under different rubric versions has
incomparable ranks.
"""
import pytest

from repositories.memory import InMemoryRubricRepository
from schemas.rubric import AnchoredCompetency, JobRubric, RubricStatus

_ANCHORS = {1: "a", 2: "b", 3: "c", 4: "d", 5: "e"}


def _rubric(rubric_id, version, job_id="job_1"):
    return JobRubric(
        rubric_id=rubric_id, job_id=job_id, version=version, status=RubricStatus.DRAFT,
        competencies=[
            AnchoredCompetency(name="A", definition="d", weight=0.4, anchors=_ANCHORS),
            AnchoredCompetency(name="B", definition="d", weight=0.3, anchors=_ANCHORS),
            AnchoredCompetency(name="C", definition="d", weight=0.3, anchors=_ANCHORS),
        ],
    )


@pytest.mark.asyncio
async def test_draft_is_not_returned_as_approved():
    repo = InMemoryRubricRepository()
    await repo.save(_rubric("rub_1", 1))
    assert await repo.get_approved_for_job("job_1") is None


@pytest.mark.asyncio
async def test_approve_makes_it_the_active_rubric():
    repo = InMemoryRubricRepository()
    await repo.save(_rubric("rub_1", 1))
    await repo.approve("rub_1")
    assert (await repo.get_approved_for_job("job_1")).rubric_id == "rub_1"


@pytest.mark.asyncio
async def test_approving_v2_supersedes_v1():
    repo = InMemoryRubricRepository()
    await repo.save(_rubric("rub_1", 1))
    await repo.approve("rub_1")
    await repo.save(_rubric("rub_2", 2))
    await repo.approve("rub_2")
    assert (await repo.get_approved_for_job("job_1")).rubric_id == "rub_2"
    assert (await repo.get("rub_1")).status is RubricStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_exactly_one_approved_rubric_per_job():
    repo = InMemoryRubricRepository()
    for i in (1, 2, 3):
        await repo.save(_rubric(f"rub_{i}", i))
        await repo.approve(f"rub_{i}")
    versions = await repo.list_versions_for_job("job_1")
    assert len([r for r in versions if r.status is RubricStatus.APPROVED]) == 1


@pytest.mark.asyncio
async def test_an_unapprovable_rubric_is_refused():
    """The approval gate is enforced at the storage boundary too, so a bad
    rubric cannot become active by bypassing the API layer."""
    repo = InMemoryRubricRepository()
    bad = _rubric("rub_bad", 1)
    bad = bad.model_copy(update={"competencies": bad.competencies[:2]})  # only 2
    await repo.save(bad)
    with pytest.raises(ValueError):
        await repo.approve("rub_bad")


@pytest.mark.asyncio
async def test_approval_does_not_leak_across_jobs():
    repo = InMemoryRubricRepository()
    await repo.save(_rubric("rub_a", 1, job_id="job_a"))
    await repo.save(_rubric("rub_b", 1, job_id="job_b"))
    await repo.approve("rub_a")
    assert await repo.get_approved_for_job("job_b") is None
