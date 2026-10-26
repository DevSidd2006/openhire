"""
Reference-isolation contract for the in-memory repositories.

A real database serialises records on write and materialises a fresh object
on every read. The in-process stubs must behave the same way, otherwise a
caller can mutate stored state without a save, without an updated_at bump
and without any of the validation that the service layer performs.
"""
import pytest

from repositories.interfaces import CandidateRecord, JobRecord
from repositories.memory import (
    InMemoryBugReportRepository,
    InMemoryCandidateRepository,
    InMemoryJobRepository,
    InMemoryRubricRepository,
)
from schemas.job import Competency, JobDescription
from schemas.resume import ParsedResume
from tests.rubric_fixtures import approved_rubric


def _job_description() -> JobDescription:
    return JobDescription(
        job_id="job_isolation",
        title="Original title",
        description="Original description",
        competencies=[
            Competency(name="Python", weight=0.5),
            Competency(name="System design", weight=0.5),
        ],
    )


@pytest.mark.asyncio
async def test_job_save_return_and_get_are_isolated_copies():
    repo = InMemoryJobRepository()
    record = JobRecord(job_id="job_isolation", job=_job_description())
    saved = await repo.save(record)

    saved.job.title = "Mutated returned copy"
    saved.job.competencies.append(Competency(name="Injected", weight=0.0))
    record.job.title = "Mutated caller copy"

    fetched = await repo.get("job_isolation")
    assert fetched.job.title == "Original title"
    assert [competency.name for competency in fetched.job.competencies] == [
        "Python",
        "System design",
    ]


@pytest.mark.asyncio
async def test_job_list_returns_isolated_copies():
    repo = InMemoryJobRepository()
    await repo.save(JobRecord(job_id="job_isolation", job=_job_description()))

    listed = await repo.list_jobs()
    listed[0].job.title = "Mutated list item"

    fetched = await repo.get("job_isolation")
    assert fetched.job.title == "Original title"


@pytest.mark.asyncio
async def test_candidate_save_and_get_are_isolated_copies():
    repo = InMemoryCandidateRepository()
    resume = ParsedResume(
        candidate_id="cand_isolation",
        candidate_name="Jane Doe",
        skills=["Python"],
    )
    saved = await repo.save(
        CandidateRecord(
            candidate_id="cand_isolation",
            user_id="user_owner",
            resume=resume,
        )
    )

    saved.resume.skills.append("Injected")
    saved.user_id = "user_attacker"

    fetched = await repo.get("cand_isolation")
    assert fetched.user_id == "user_owner"
    assert fetched.resume.skills == ["Python"]


@pytest.mark.asyncio
async def test_rubric_save_and_get_are_isolated_copies():
    repo = InMemoryRubricRepository()
    rubric = approved_rubric(job_id="job_isolation")
    saved = await repo.save(rubric)

    saved.competencies[0].name = "Mutated competency"
    saved.status = "superseded"

    fetched = await repo.get(rubric.rubric_id)
    assert fetched.competencies[0].name == "Python"
    assert fetched.status.value == "approved"

    approved = await repo.get_approved_for_job("job_isolation")
    approved.competencies[0].name = "Mutated again"
    assert (await repo.get(rubric.rubric_id)).competencies[0].name == "Python"


@pytest.mark.asyncio
async def test_bug_report_reads_are_isolated_copies():
    """A record with no nested mutable fields still must not leak its
    stored instance through get()."""
    repo = InMemoryBugReportRepository()
    from repositories.interfaces import BugReportRecord

    record = BugReportRecord(
        bug_id="bug_isolation",
        title="Original title",
        description="Original description",
        reporter_user_id="user_reporter",
    )
    saved = await repo.save(record)
    saved.title = "Mutated"

    fetched = await repo.get("bug_isolation")
    assert fetched.title == "Original title"
