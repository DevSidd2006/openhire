"""
OpenBox use cases: filing and triaging platform bug reports.

OpenBox (pages/openbox.html) is a shared, platform-wide feed - every
authenticated user reads every report - not a per-user inbox, so this
service has no ownership-scoped listing the way `CandidateService` does.
Only status transitions are gated (to recruiters, at the route layer via
`require_scopes("recruiter:write")` - see api/routes/bugs.py), matching the
shortlist/reject pattern `ApplicationService` already established.
"""
from __future__ import annotations

import uuid
from typing import Optional

from core.errors import NotFoundError
from core.logging import get_logger
from repositories.interfaces import (
    BugReportRecord,
    BugReportRepository,
    BugSeverity,
    BugStatus,
)

logger = get_logger("services.bug_report")


class BugReportService:
    """Use cases for OpenBox bug reports."""

    def __init__(self, *, bug_report_repository: BugReportRepository) -> None:
        self._reports = bug_report_repository

    async def file_report(
        self,
        *,
        title: str,
        description: str,
        reporter_user_id: str,
        severity: BugSeverity = BugSeverity.MEDIUM,
        page: Optional[str] = None,
    ) -> BugReportRecord:
        """File a new bug report. `bug_id` is generated here (uuid4,
        matching `CandidateService.register_candidate`'s id generation),
        never accepted from the caller."""
        record = BugReportRecord(
            bug_id=f"bug_{uuid.uuid4().hex[:8]}",
            reporter_user_id=reporter_user_id,
            title=title,
            description=description,
            severity=severity,
            page=page,
        )
        saved = await self._reports.save(record)
        logger.info(
            "bug report filed",
            extra={"event": "bug_report_filed", "bug_id": saved.bug_id, "severity": severity.value},
        )
        return saved

    async def list_reports(self) -> list[BugReportRecord]:
        """Every report, newest first - OpenBox's own feed."""
        return await self._reports.list_all()

    async def get_report(self, bug_id: str) -> BugReportRecord:
        record = await self._reports.get(bug_id)
        if record is None:
            raise NotFoundError(
                "Bug report not found", internal_detail=f"bug_id={bug_id!r}"
            )
        return record

    async def update_status(self, bug_id: str, status: BugStatus) -> BugReportRecord:
        """Recruiter-only triage action (enforced at the route layer -
        see api/routes/bugs.py). Idempotent: setting a report to the
        status it already has is a no-op success."""
        record = await self.get_report(bug_id)
        updated = record.model_copy(update={"status": status})
        saved = await self._reports.save(updated)
        logger.info(
            "bug report status changed",
            extra={"event": "bug_report_status_changed", "bug_id": bug_id, "status": status.value},
        )
        return saved
