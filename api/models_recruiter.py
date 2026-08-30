"""
Recruiter-facing API models (Chunk 5).

Every domain fact here is read straight from an existing record - no new
domain schema is introduced, and `CandidateReport`/`CandidateLeaderboard`
(schemas/scoring.py) are reused verbatim wherever a recruiter needs the
full evaluation/ranking detail (see api/routes/evaluations.py's precedent:
reuse the domain schema directly when nothing in it needs redacting for
this caller).

Two views ARE narrowed, deliberately, and only here:

  `CandidateSummaryView`   A recruiter scanning many applications for one
                           job does not need each row to carry the
                           candidate's full `ParsedResume` (work history,
                           projects, raw_text) - that is already available,
                           unabridged, via the existing `GET /candidates/{id}`
                           (Chunk 2). This is the triage-sized subset.
  `InterviewStatusView`    A recruiter checking interview progress does not
                           need the embedded `InterviewState`/job/resume
                           snapshot `SessionRecord` carries for restoration
                           (Chunk 3) - that machinery is not recruiter-facing
                           data at all, just persistence plumbing.

Both narrowings are Step 13's data-exposure discipline applied deliberately,
not a removal of anything the candidate-facing or restoration paths depend
on - those paths are untouched and keep reading the full records directly.
"""
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from pydantic import BaseModel

from repositories.interfaces import (
    Application,
    CandidateRecord,
    EvaluationJob,
    EvaluationStatus,
    SessionRecord,
)
from schemas.scoring import CandidateLeaderboard

if TYPE_CHECKING:
    from utils.interview_session import SessionStatus


class CandidateSummaryView(BaseModel):
    """Enough to triage a list of applicants - not the full profile."""

    candidate_id: str
    candidate_name: str
    email: Optional[str] = None
    skills: List[str] = []
    total_experience_years: Optional[float] = None

    @classmethod
    def from_record(cls, record: CandidateRecord) -> "CandidateSummaryView":
        resume = record.resume
        return cls(
            candidate_id=record.candidate_id,
            candidate_name=resume.candidate_name,
            email=resume.email,
            skills=list(resume.skills),
            total_experience_years=resume.total_experience_years,
        )


class InterviewStatusView(BaseModel):
    """A recruiter-facing summary of one interview session - the SAME
    `SessionStatus` enum the candidate-facing `/sessions/{id}` endpoints
    already use (Chunk 1/3), not a second state machine."""

    session_id: str
    status: "SessionStatus"
    questions_asked: int
    questions_answered: int
    termination_reason: Optional[str] = None

    @classmethod
    def from_record(cls, record: SessionRecord) -> "InterviewStatusView":
        return cls(
            session_id=record.session_id,
            status=record.status,
            questions_asked=record.questions_asked,
            questions_answered=record.questions_answered,
            termination_reason=record.termination_reason,
        )


class ApplicationOverview(BaseModel):
    """One row of a recruiter's "applications for this job" view -
    `Application` (schemas/application.py) is reused verbatim (it already
    carries `status`/`matching_score`/`session_id`), with the candidate
    summary, interview status and evaluation status assembled alongside it
    by `RecruiterService` in one batched pass (never N+1 - see that
    service's docstring).

    `evaluation_status`/`evaluation_id` only - never the full
    `CandidateReport` here: that would mean embedding one full report per
    row of a list response. The complete result is one call away via
    `GET /evaluations/{evaluation_id}` or
    `GET /applications/{application_id}/evaluation`.
    """

    application: Application
    candidate: Optional[CandidateSummaryView] = None
    interview_status: Optional[InterviewStatusView] = None
    evaluation_id: Optional[str] = None
    evaluation_status: Optional[EvaluationStatus] = None


class ApplicationOverviewListResponse(BaseModel):
    job_id: str
    applications: List[ApplicationOverview]
    total: int

    @classmethod
    def from_domain(cls, job_id: str, overviews: List[ApplicationOverview]) -> "ApplicationOverviewListResponse":
        return cls(job_id=job_id, applications=overviews, total=len(overviews))


class ApplicationInterviewResponse(BaseModel):
    """`GET /applications/{application_id}/interview`'s body.
    `interview=None` means no session has been linked to this application
    yet - a normal, valid state, not an error (matches
    `SessionEvaluationResponse`'s `evaluation=None` precedent, Chunk 4)."""

    application_id: str
    interview: Optional[InterviewStatusView] = None


class ApplicationEvaluationResponse(BaseModel):
    """`GET /applications/{application_id}/evaluation`'s body - the
    application-centric equivalent of Chunk 4's
    `GET /sessions/{session_id}/evaluation`, for a recruiter who thinks in
    terms of "this application" rather than "this session"."""

    application_id: str
    evaluation: Optional[EvaluationJob] = None


class LeaderboardResponse(BaseModel):
    """`GET /jobs/{job_id}/leaderboard`'s body.

    `leaderboard` is the EXISTING `CandidateLeaderboard`
    (schemas/scoring.py), produced by the EXISTING `LeaderboardAgent` -
    reused verbatim, never recomputed. `entries` on it reflects whatever
    filter/pagination was requested; `top_candidates`,
    `total_candidates`/`strong_candidates`/`candidates`/`requires_review`
    always describe the FULL, unfiltered ranked population, so those
    summary numbers stay meaningful regardless of which page or filter a
    caller is looking at.
    """

    leaderboard: CandidateLeaderboard
    # How many entries matched the requested filter, BEFORE pagination -
    # what a caller needs to build "page N of M".
    total_matching: int
    limit: Optional[int] = None
    offset: int = 0
    generated_at: datetime


__all__ = [
    "ApplicationEvaluationResponse",
    "ApplicationInterviewResponse",
    "ApplicationOverview",
    "ApplicationOverviewListResponse",
    "CandidateSummaryView",
    "InterviewStatusView",
    "LeaderboardResponse",
]
