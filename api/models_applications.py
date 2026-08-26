"""
Application API request/response models.

`Application` (schemas/application.py) is nested directly - see that
module's docstring for why a candidate/recruiter-facing `Application` has
no field that needs hiding the way `InterviewQuestion.reason` does.
"""
from typing import List

from pydantic import BaseModel, ConfigDict, Field

from schemas.application import Application


# ---------------------------------------------------------------------------
# POST /applications
# ---------------------------------------------------------------------------

class CreateApplicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)


class ApplicationResponse(BaseModel):
    application: Application

    @classmethod
    def from_domain(cls, application: Application) -> "ApplicationResponse":
        return cls(application=application)


# ---------------------------------------------------------------------------
# GET /applications, GET /jobs/{job_id}/shortlist
# ---------------------------------------------------------------------------

class ApplicationListResponse(BaseModel):
    applications: List[Application]
    total: int

    @classmethod
    def from_domain(cls, applications: List[Application]) -> "ApplicationListResponse":
        return cls(applications=applications, total=len(applications))


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/match
# ---------------------------------------------------------------------------

class MatchingRunResponse(BaseModel):
    job_id: str
    matched: int
    shortlisted: int
    rejected: int
    failed_candidate_ids: List[str]
    applications: List[Application]


__all__ = [
    "ApplicationListResponse",
    "ApplicationResponse",
    "CreateApplicationRequest",
    "MatchingRunResponse",
]
