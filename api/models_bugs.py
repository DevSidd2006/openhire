"""
OpenBox (bug report) API request/response models.

`BugReportRecord` (repositories/interfaces.py) is nested directly, the same
pattern api/models_applications.py uses for `Application` - nothing here
hides a field, so no separate response DTO is needed.
"""
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from repositories.interfaces import BugReportRecord, BugSeverity, BugStatus


# ---------------------------------------------------------------------------
# POST /bugs
# ---------------------------------------------------------------------------

class FileBugReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)
    severity: BugSeverity = BugSeverity.MEDIUM
    # The page the reporter was on when they hit the bug (e.g.
    # "leaderboard.html") - optional context, never validated against a
    # known page list since new pages should never make reporting fail.
    page: Optional[str] = Field(default=None, max_length=100)


class BugReportResponse(BaseModel):
    bug_report: BugReportRecord

    @classmethod
    def from_domain(cls, record: BugReportRecord) -> "BugReportResponse":
        return cls(bug_report=record)


# ---------------------------------------------------------------------------
# GET /bugs
# ---------------------------------------------------------------------------

class BugReportListResponse(BaseModel):
    bug_reports: List[BugReportRecord]
    total: int

    @classmethod
    def from_domain(cls, records: List[BugReportRecord]) -> "BugReportListResponse":
        return cls(bug_reports=records, total=len(records))


# ---------------------------------------------------------------------------
# PATCH /bugs/{bug_id}/status
# ---------------------------------------------------------------------------

class UpdateBugStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: BugStatus


__all__ = [
    "BugReportListResponse",
    "BugReportResponse",
    "FileBugReportRequest",
    "UpdateBugStatusRequest",
]
