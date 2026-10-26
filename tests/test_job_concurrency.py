"""Exercise shared request locks with controlled repository interleavings."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import Request

from core.dependencies import get_job_service
from core.errors import ConflictError
from repositories.interfaces import JobRecord
from repositories.memory import InMemoryJobRepository
from schemas.job import JobDescription
from services.job_service import JobPatchValidationError


class PausingJobRepository(InMemoryJobRepository):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Queue()
        self.release = asyncio.Event()

    async def get(self, job_id):
        record = await super().get(job_id)
        await self.entered.put(("get", job_id))
        await self.release.wait()
        return record

    async def archive(self, job_id):
        await self.entered.put(("archive", job_id))
        await self.release.wait()
        return await super().archive(job_id)


async def request_services(repo, *job_ids):
    for job_id in job_ids:
        await repo.save(JobRecord(
            job_id=job_id,
            job=JobDescription(job_id=job_id, title="Original", description="Sample role"),
        ))
    container = SimpleNamespace(
        job_repository=repo,
        rubric_repository=None,
        jd_analyzer_factory_for=lambda app: None,
        job_locks={},
        job_locks_guard=asyncio.Lock(),
    )
    app = SimpleNamespace(state=SimpleNamespace(container=container))
    # Resolve separately, just as two HTTP requests do.
    return tuple(get_job_service(Request({"type": "http", "app": app})) for _ in range(2))


@pytest.mark.asyncio
async def test_concurrent_job_updates_preserve_both_fields():
    repo = PausingJobRepository()
    first, second = await request_services(repo, "job_1")
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(first.update_job("job_1", {"department": "Platform"}))
        assert await asyncio.wait_for(repo.entered.get(), 1) == ("get", "job_1")
        tasks.create_task(second.update_job("job_1", {"title": "Updated"}))
        try:
            await asyncio.sleep(0)  # Let the second request attempt its read.
            assert repo.entered.empty(), "Second request read a stale snapshot"
        finally:
            repo.release.set()

    final = await repo.get("job_1")
    assert final.job.department == "Platform"
    assert final.job.title == "Updated"


@pytest.mark.asyncio
async def test_concurrent_updates_to_different_jobs_do_not_interfere():
    repo = PausingJobRepository()
    first, second = await request_services(repo, "job_1", "job_2")
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(first.update_job("job_1", {"title": "Updated Role 1"}))
        tasks.create_task(second.update_job("job_2", {"title": "Updated Role 2"}))
        try:
            entered = [await asyncio.wait_for(repo.entered.get(), 1) for _ in range(2)]
            assert set(entered) == {("get", "job_1"), ("get", "job_2")}
        finally:
            repo.release.set()

    assert (await repo.get("job_1")).job.title == "Updated Role 1"
    assert (await repo.get("job_2")).job.title == "Updated Role 2"


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_first", [False, True])
async def test_update_and_archive_share_the_request_lock(archive_first):
    repo = PausingJobRepository()
    first, second = await request_services(repo, "job_1")
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(
            first.archive_job("job_1") if archive_first
            else first.update_job("job_1", {"title": "Updated"})
        )
        await asyncio.wait_for(repo.entered.get(), 1)

        async def following_request():
            if archive_first:
                with pytest.raises(ConflictError):
                    await second.update_job("job_1", {"title": "Updated"})
            else:
                await second.archive_job("job_1")

        tasks.create_task(following_request())
        try:
            await asyncio.sleep(0)
            assert repo.entered.empty(), "Update and archive did not serialize"
        finally:
            repo.release.set()

    final = await repo.get("job_1")
    assert final.is_active is False
    assert final.job.title == ("Original" if archive_first else "Updated")


@pytest.mark.asyncio
async def test_validation_failure_releases_lock_for_next_request():
    repo = PausingJobRepository()
    first, second = await request_services(repo, "job_1")
    repo.release.set()
    with pytest.raises(JobPatchValidationError):
        await first.update_job("job_1", {"openings": None})

    updated = await asyncio.wait_for(second.update_job("job_1", {"title": "Updated"}), 1)
    assert updated.job.title == "Updated"
    assert updated.job.openings == 1
