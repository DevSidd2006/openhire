import asyncio, pytest
from repositories.interfaces import JobRecord
from repositories.memory import InMemoryJobRepository
from schemas.job import JobDescription
from services.job_service import JobService

class ConcurrentTestRepo(InMemoryJobRepository):
    async def get(self, job_id):
        res = await super().get(job_id)
        await asyncio.sleep(0.01)
        return res


@pytest.mark.asyncio
async def test_concurrent_job_updates_preserve_both_fields():
    repo = ConcurrentTestRepo()
    record = JobRecord(job_id = "job_rate_test",
    job = JobDescription(
        job_id = "job_rare_test",
        title = "Original",
        description = "Sample role",
    ),
    )

    await repo.save(record)
    service = JobService(job_repository = repo)

    await asyncio.gather(
        service.update_job(record.job_id, {"department": "Platform"}),
        service.update_job(record.job_id, {"title": "Updated"}),
    )

    final = await repo.get(record.job_id)

    assert final.job.department == "Platform", "Department edit was lost!"
    assert final.job.title == "Updated", "Title edit was lost!"

@pytest.mark.asyncio
async def test_concurrent_updates_to_different_jobs_do_not_interfere():
    repo = ConcurrentTestRepo()
    job_1 = JobRecord(
        job_id = "job_1",
        job = JobDescription(job_id = "job_1", title = "Role 1", description = "Desc 1"),
    )
    job_2 = JobRecord(
        job_id = "job_2",
        job = JobDescription(job_id = "job_2", title = "Role 2", description = "Desc 2"),
    )
    await repo.save(job_1)
    await repo.save(job_2)
    service = JobService(job_repository = repo)

    await asyncio.gather(
        service.update_job("job_1", {"title": "Updated Role 1"}),
        service.update_job("job_2", {"title": "Updated Role 2"}),
    )

    res_1 = await repo.get("job_1")
    res_2 = await repo.get("job_2")

    assert res_1.job.title == "Updated Role 1"
    assert res_2.job.title == "Updated Role 2"



