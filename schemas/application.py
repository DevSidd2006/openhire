"""
Application schema - the candidate-applying-to-a-job concept.

Not represented anywhere in the codebase before this chunk. `schemas/job.py`
and `schemas/resume.py` model a job and a candidate's resume in isolation;
nothing links the two into "candidate X applied to job Y" - that link is
what an interview session (utils/interview_session.py) currently takes for
granted, via the candidate_id/job_id pair passed straight into
`InterviewSessionRunner`. Chunk 2 introduces `Application` as that missing
link, per its own explicit brief (Candidate -> Application -> Job ->
Interview -> Evaluation).

Terminology note for the database owner: docs/roles/database.md's Stage-1
plan has `jobs`, `candidates` and `interviews` tables, with no `applications`
table - the candidate/job/interview link is currently planned as a single
`interviews` row (job_id, candidate_id, link_token, status). This schema is
additive backend-application-layer scaffolding, not a demand that the
database add a fourth table; `Application` naturally maps onto extra columns
on that same `interviews` row (or a table of its own) once the schema is
finalized - either shape can satisfy `ApplicationRepository`
(repositories/interfaces.py). Flagged explicitly in the Chunk 2 report for
you to reconcile with the database teammate.

Deliberately minimal: no cover letter, no attachments, no source-channel
tracking - nothing here is invented beyond what Chunk 2's own brief and the
existing matching/shortlisting code already justify. `matching_score` reuses
`schemas.evaluation.MatchingScore` verbatim (never a duplicate
representation); `session_id` is the bridge to the existing, unmodified
`/sessions` API from Chunk 1.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from schemas.evaluation import MatchingScore


class ApplicationStatus(str, Enum):
    """Lifecycle of one application.

    Mirrors the flow Chunk 2's brief lays out: Job -> Applications ->
    Matching -> Ranked candidates -> Shortlisted candidates -> Interview.

    SUBMITTED    candidate has applied; no matching computed yet.
    SHORTLISTED  matching ran and ResumeMatcherAgent's own
                 `shortlist_recommendation` was True (services/matching_service.py) -
                 not a separately invented threshold.
    REJECTED     matching ran and the recommendation was False.
    INTERVIEW_LINKED  an interview session (Chunk 1's /sessions) has been
                 created for this application - see
                 services/application_service.py:link_interview_session.
    """

    SUBMITTED = "submitted"
    SHORTLISTED = "shortlisted"
    REJECTED = "rejected"
    INTERVIEW_LINKED = "interview_linked"


class Application(BaseModel):
    """One candidate's application to one job.

    Storage-shaped like `InterviewTranscript` (schemas/interview.py) already
    is for `TranscriptRepository` - this model IS the repository record,
    with no separate wrapper DTO, because (unlike a session's
    `InterviewSessionRunner`) there is no live runtime object this needs to
    be projected from.
    """

    model_config = ConfigDict(frozen=False)

    application_id: str
    job_id: str
    candidate_id: str
    status: ApplicationStatus = ApplicationStatus.SUBMITTED

    # Populated once services/matching_service.py has run for this
    # application's job. None before that - never a fabricated score.
    matching_score: Optional[MatchingScore] = None

    # The linked interview session's id, once one exists (Chunk 1's
    # /sessions, unmodified). None until `link_interview_session` sets it.
    session_id: Optional[str] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None


__all__ = ["Application", "ApplicationStatus"]
