"""
Candidate use cases.

Wraps the existing `ResumeParserAgent` (agents/resume_parser/agent.py) -
raw resume text goes in, a structured `ParsedResume` (schemas/resume.py)
comes out, exactly as `orchestration/graph.py:node_parse_resumes` already
does. `ParsedResume` is the candidate profile used everywhere else in this
codebase (the interview engine, the matching agent, the evaluators all key
off `ParsedResume.candidate_id`); this service does not introduce a second,
competing "Candidate" representation.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from agents.resume_parser.agent import ResumeParserAgent
from core.errors import DependencyError, ForbiddenError, NotFoundError
from core.logging import get_logger, log_context
from repositories.interfaces import CandidateRecord, CandidateRepository
from schemas.resume import ParsedResume

logger = get_logger("services.candidate")


class CandidateService:
    """Use cases for candidate profiles."""

    def __init__(
        self,
        *,
        candidate_repository: CandidateRepository,
        resume_parser_factory=None,
    ) -> None:
        self._candidates = candidate_repository
        self._resume_parser_factory = resume_parser_factory

    async def register_candidate(
        self,
        *,
        resume_text: str,
        candidate_name: str,
        user_id: str,
        candidate_id: Optional[str] = None,
    ) -> CandidateRecord:
        """Parse raw resume text into a `ParsedResume` and store it.

        `candidate_id` is generated (uuid4, matching `JobService.create_job`'s
        job_id generation) when not supplied - never invented by the parser,
        which takes it as an input.

        `user_id` links the candidate to the authenticated user who owns it.
        """
        candidate_id = candidate_id or f"cand_{uuid.uuid4().hex[:8]}"
        parsed_resume, used_fallback, warning = await self._parse(
            resume_text=resume_text, candidate_id=candidate_id, candidate_name=candidate_name,
        )

        record = CandidateRecord(
            candidate_id=candidate_id,
            user_id=user_id,
            resume=parsed_resume,
            used_fallback=used_fallback,
            parse_warning=warning,
        )
        stored = await self._candidates.save(record)
        logger.info(
            "candidate registered",
            extra=log_context(
                event="candidate_registered", candidate_id=candidate_id,
                user_id=user_id,
                used_fallback=used_fallback,
            ),
        )
        return stored

    async def preview_resume(
        self, *, resume_text: str, candidate_name: str
    ) -> tuple[ParsedResume, bool, Optional[str]]:
        """Parse raw resume text via the existing `ResumeParserAgent` and
        return the result WITHOUT persisting anything.

        Backs the resume-upload auto-fill flow (`POST
        /candidates/parse-resume-file`, api/routes/candidates.py): a
        candidate needs to see and edit the extraction before a `Candidate`
        record is actually created, so this calls the exact same `_parse`
        helper `register_candidate` uses, just without the `save()` at the
        end. `candidate_id` is a throwaway value - the agent takes it as an
        input (for its own logging/run-id) but nothing here stores it, so
        it never collides with a real candidate_id.
        """
        preview_id = f"preview_{uuid.uuid4().hex[:8]}"
        return await self._parse(
            resume_text=resume_text, candidate_id=preview_id, candidate_name=candidate_name,
        )

    async def get_candidate(self, candidate_id: str) -> CandidateRecord:
        record = await self._candidates.get(candidate_id)
        if record is None:
            raise NotFoundError(
                "No candidate found for the given candidate_id",
                internal_detail=f"candidate record missing for candidate_id={candidate_id!r}",
            )
        return record

    async def validate_candidate_ownership(self, candidate_id: str, user_id: str) -> CandidateRecord:
        """Get a candidate and validate that it belongs to the authenticated user.

        Raises ForbiddenError if the candidate does not belong to this user.
        """
        record = await self.get_candidate(candidate_id)
        if record.user_id != user_id:
            raise ForbiddenError(
                "You do not have permission to access this candidate",
                internal_detail=f"candidate {candidate_id!r} owned by {record.user_id!r}, not {user_id!r}",
            )
        return record

    async def get_for_user(self, user_id: str) -> Optional[CandidateRecord]:
        """This user's own candidate profile, if they have registered one.

        Backs `GET /candidates/me` - the fix for a real bug: the frontend
        only ever learned `candidate_id` at the moment `POST /candidates`
        succeeded (apply.html) and cached it in localStorage; logging back
        in overwrote that cached user object without it (login.html only
        ever stored name/role/user_id), so a returning candidate's own
        dashboard could not find their candidate_id at all and showed zero
        applications even though the application was still there. Newest
        first, first element returned, since one user registering a second
        candidate profile is not a flow this app offers today - but
        `list_for_user` (the repository method this wraps) already returns
        every match, so nothing here assumes exactly one.
        """
        matches = await self._candidates.list_for_user(user_id)
        return matches[0] if matches else None

    async def list_candidates(self, *, include_hidden: bool = False) -> list[CandidateRecord]:
        return await self._candidates.list_candidates(include_hidden=include_hidden)

    async def update_candidate(
        self,
        candidate_id: str,
        *,
        resume_text: Optional[str] = None,
        patch: Optional[Dict[str, Any]] = None,
    ) -> CandidateRecord:
        """Update a candidate's profile.

        Two independent, composable operations:
          - `resume_text` set: re-parse it via `ResumeParserAgent` (the
            candidate submitted a new/updated resume) and replace the stored
            `ParsedResume` with the fresh result.
          - `patch` set: apply direct field edits (skills, summary, contact
            details, ...) on top of whichever `ParsedResume` is current at
            that point - the freshly re-parsed one if `resume_text` was also
            given, otherwise the one already on file.

        Both may be given together (re-parse, then apply corrections on top
        of it); `patch` only ever contains fields the caller explicitly set
        (see api/models_candidates.py), so an omitted field is left exactly
        as stored.
        """
        record = await self.get_candidate(candidate_id)
        resume = record.resume
        used_fallback = record.used_fallback
        warning = record.parse_warning

        if resume_text is not None:
            resume, used_fallback, warning = await self._parse(
                resume_text=resume_text,
                candidate_id=candidate_id,
                candidate_name=record.resume.candidate_name,
            )

        if patch:
            merged = resume.model_dump()
            merged.update(patch)
            resume = ParsedResume(**merged)

        updated_record = record.model_copy(
            update={"resume": resume, "used_fallback": used_fallback, "parse_warning": warning}
        )
        stored = await self._candidates.save(updated_record)
        logger.info(
            "candidate updated",
            extra=log_context(event="candidate_updated", candidate_id=candidate_id),
        )
        return stored

    async def _parse(
        self, *, resume_text: str, candidate_id: str, candidate_name: str
    ) -> tuple[ParsedResume, bool, Optional[str]]:
        parser = self._resume_parser_factory() if self._resume_parser_factory else ResumeParserAgent()

        agent_result = await parser.run(
            run_id=f"candidate_parse_{candidate_id}",
            resume_text=resume_text,
            candidate_id=candidate_id,
            candidate_name=candidate_name,
        )
        result = (agent_result or {}).get("result") or {}
        parsed_resume = result.get("parsed_resume")

        if parsed_resume is None:
            # ResumeParserAgent already has its own deterministic fallback
            # for a bad/failed LLM call (see its module docstring) - a
            # missing parsed_resume here means even THAT failed, a genuine
            # unexpected error, not something to paper over.
            error_reason = result.get("error") or (agent_result or {}).get("error")
            logger.error(
                "resume parsing failed: %s",
                error_reason,
                extra=log_context(event="resume_parse_failed", candidate_id=candidate_id),
            )
            raise DependencyError(
                "The resume could not be parsed. Please try again.",
                internal_detail=f"ResumeParserAgent failed for candidate_id={candidate_id!r}: {error_reason}",
                context={"candidate_id": candidate_id},
            )

        return parsed_resume, bool(result.get("used_fallback")), result.get("error")


__all__ = ["CandidateService"]
