
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from repositories.interfaces import CandidateRecord
from schemas.resume import ParsedResume
from schemas.types import NonEmptyStr


class RegisterCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resume_text: NonEmptyStr = Field(max_length=50_000)
    candidate_name: NonEmptyStr = Field(max_length=200)
    candidate_id: Optional[str] = Field(default=None, min_length=1)


class CandidateResponse(BaseModel):
    candidate_id: str
    resume: ParsedResume
    used_fallback: bool
    parse_warning: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    is_hidden: bool = False

    @classmethod
    def from_record(cls, record: CandidateRecord) -> "CandidateResponse":
        return cls(
            candidate_id=record.candidate_id,
            resume=record.resume,
            used_fallback=record.used_fallback,
            parse_warning=record.parse_warning,
            created_at=record.created_at,
            updated_at=record.updated_at,
            is_hidden=record.is_hidden,
        )


class CandidateListResponse(BaseModel):
    candidates: List[CandidateResponse]
    total: int

    @classmethod
    def from_records(
        cls, records: List[CandidateRecord]
    ) -> "CandidateListResponse":
        return cls(
            candidates=[CandidateResponse.from_record(record) for record in records],
            total=len(records),
        )


class UpdateCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resume_text: Optional[NonEmptyStr] = Field(
        default=None, max_length=50_000
    )
    candidate_name: Optional[NonEmptyStr] = Field(
        default=None, max_length=200
    )
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    summary: Optional[str] = None
    skills: Optional[List[str]] = None
    technologies: Optional[List[str]] = None
    languages: Optional[List[str]] = None
    total_experience_years: Optional[float] = None


class ParseResumeFileResponse(BaseModel):
    parsed_resume: ParsedResume
    resume_text: str
    used_fallback: bool
    parse_warning: Optional[str] = None


__all__ = [
    "CandidateListResponse",
    "CandidateResponse",
    "ParseResumeFileResponse",
    "RegisterCandidateRequest",
    "UpdateCandidateRequest",
]
