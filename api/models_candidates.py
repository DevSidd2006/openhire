"""
Candidate API request/response models.

Same convention as api/models_jobs.py: `ParsedResume` (schemas/resume.py)
comes straight from the existing schema and is nested directly in
responses - it is the candidate's own profile, so no narrowing view is
needed the way `QuestionView` narrows `InterviewQuestion` for a candidate
mid-interview.
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from repositories.interfaces import CandidateRecord
from schemas.resume import ParsedResume


# ---------------------------------------------------------------------------
# POST /candidates
# ---------------------------------------------------------------------------

class RegisterCandidateRequest(BaseModel):
    """Raw resume text is parsed via the existing `ResumeParserAgent`
    (services/candidate_service.py)."""

    model_config = ConfigDict(extra="forbid")

    resume_text: str = Field(min_length=1, max_length=50_000)
    candidate_name: str = Field(min_length=1, max_length=200)
    # Optional caller-supplied id. Generated (cand_<uuid8>) if omitted - see
    # CandidateService.register_candidate.
    candidate_id: Optional[str] = Field(default=None, min_length=1)


class CandidateResponse(BaseModel):
    candidate_id: str
    resume: ParsedResume
    used_fallback: bool
    parse_warning: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    @classmethod
    def from_record(cls, record: CandidateRecord) -> "CandidateResponse":
        return cls(
            candidate_id=record.candidate_id, resume=record.resume,
            used_fallback=record.used_fallback, parse_warning=record.parse_warning,
            created_at=record.created_at, updated_at=record.updated_at,
        )


# ---------------------------------------------------------------------------
# GET /candidates
# ---------------------------------------------------------------------------

class CandidateListResponse(BaseModel):
    candidates: List[CandidateResponse]
    total: int

    @classmethod
    def from_records(cls, records: List[CandidateRecord]) -> "CandidateListResponse":
        return cls(
            candidates=[CandidateResponse.from_record(r) for r in records], total=len(records)
        )


# ---------------------------------------------------------------------------
# PATCH /candidates/{candidate_id}
# ---------------------------------------------------------------------------

class UpdateCandidateRequest(BaseModel):
    """Two independent, composable operations - see
    CandidateService.update_candidate for exactly how they combine:

      - `resume_text` set: re-parse via `ResumeParserAgent`.
      - any other field set: a direct edit applied on top of whichever
        `ParsedResume` is current (freshly re-parsed, if `resume_text` was
        also given).
    """

    model_config = ConfigDict(extra="forbid")

    resume_text: Optional[str] = Field(default=None, min_length=1, max_length=50_000)

    candidate_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    summary: Optional[str] = None
    skills: Optional[List[str]] = None
    technologies: Optional[List[str]] = None
    languages: Optional[List[str]] = None
    total_experience_years: Optional[float] = None


__all__ = [
    "CandidateListResponse",
    "CandidateResponse",
    "RegisterCandidateRequest",
    "UpdateCandidateRequest",
]
