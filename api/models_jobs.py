"""
Job API request/response models.

Transport-layer DTOs only, following the same convention api/models.py
established for sessions: domain data (`JobDescription`, schemas/job.py)
comes straight from the existing schema and is never duplicated here.
Unlike `QuestionView`/`EvidenceView` in api/models.py, no narrowing view is
needed - `JobDescription` has no field a caller shouldn't see (it IS the
public job posting), so responses nest it directly.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from repositories.interfaces import JobRecord
from schemas.job import JobDescription


# ---------------------------------------------------------------------------
# POST /jobs
# ---------------------------------------------------------------------------

class CreateJobRequest(BaseModel):
    """Raw job-description text is analyzed via the existing `JDAnalyzerAgent`
    (services/job_service.py) - this endpoint does not accept a
    pre-structured `JobDescription` directly, so there is exactly one path
    a job enters the system through and one thing that can drift from the
    LLM's own extraction.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=20_000)
    # Optional caller-supplied id. Generated (job_<uuid8>) if omitted - see
    # JobService.create_job.
    job_id: Optional[str] = Field(default=None, min_length=1)


class JobResponse(BaseModel):
    job_id: str
    job: JobDescription
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime] = None

    @classmethod
    def from_record(cls, record: JobRecord) -> "JobResponse":
        return cls(
            job_id=record.job_id, job=record.job, is_active=record.is_active,
            created_at=record.created_at, updated_at=record.updated_at,
        )


# ---------------------------------------------------------------------------
# GET /jobs
# ---------------------------------------------------------------------------

class JobListResponse(BaseModel):
    jobs: List[JobResponse]
    total: int

    @classmethod
    def from_records(cls, records: List[JobRecord]) -> "JobListResponse":
        return cls(jobs=[JobResponse.from_record(r) for r in records], total=len(records))


# ---------------------------------------------------------------------------
# PATCH /jobs/{job_id}
# ---------------------------------------------------------------------------

class UpdateJobRequest(BaseModel):
    """A partial edit of a stored `JobDescription`.

    Every field optional; only fields the caller actually sets are applied
    (`model_dump(exclude_unset=True)` in the route handler) - an omitted
    field is left exactly as stored, never reset to its schema default.
    Does not re-invoke `JDAnalyzerAgent` - see JobService.update_job for why.
    """

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    description: Optional[str] = None
    department: Optional[str] = None
    level: Optional[str] = None
    required_skills: Optional[List[str]] = None
    preferred_skills: Optional[List[str]] = None
    required_qualifications: Optional[List[str]] = None
    preferred_qualifications: Optional[List[str]] = None
    experience_years: Optional[int] = None
    responsibilities: Optional[List[str]] = None
    competencies: Optional[List[Dict[str, Any]]] = None
    interview_topics: Optional[List[str]] = None


__all__ = [
    "CreateJobRequest",
    "JobListResponse",
    "JobResponse",
    "UpdateJobRequest",
]
