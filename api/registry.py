"""
P5 Phase 3: in-memory session registry.

A safe abstraction over "where do running InterviewSessionRunner instances
live between HTTP requests" - deliberately small and swappable (a
database-backed registry could implement the same three methods later)
rather than a bare dict scattered across route modules. No database, no
Redis - this is local, in-process state only, matching P5's scope.
"""
import asyncio
import uuid
from typing import Dict

from utils.interview_session import InterviewSessionRunner


class SessionNotFoundError(Exception):
    """No session exists for the given session_id - either it was never
    created or was already removed. Distinct from InterviewSessionError
    (utils/interview_session.py), which is about an EXISTING session being
    in the wrong lifecycle state - this is about the session not existing
    in the registry at all (maps to HTTP 404, not 409)."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"No session found for session_id={session_id!r}")


class SessionRegistry:
    """In-memory session_id -> InterviewSessionRunner registry.

    session_id is generated here (uuid4 hex - 122 bits of randomness, not
    guessable/sequential, P5 Phase 21) and is the ONLY key used to look up a
    session; candidate_id/job_id live on the runner/job/resume and are never
    used for lookup, so one candidate's session_id can never resolve to a
    different candidate's runner (P5 Phase 9: session isolation).

    Guarded by a single asyncio.Lock around registry mutations/reads -
    plain dict get/set is already atomic under the GIL, but this makes the
    "concurrent HTTP requests must not corrupt the registry" guarantee
    (P5 Phase 10) explicit and independent of that implementation detail.
    Not distributed locking - this is a single in-process registry, exactly
    as P5 scopes it.
    """

    def __init__(self):
        self._sessions: Dict[str, InterviewSessionRunner] = {}
        self._lock = asyncio.Lock()

    async def create_session(self, runner: InterviewSessionRunner) -> str:
        session_id = uuid.uuid4().hex
        async with self._lock:
            self._sessions[session_id] = runner
        return session_id

    async def get_session(self, session_id: str) -> InterviewSessionRunner:
        async with self._lock:
            runner = self._sessions.get(session_id)
        if runner is None:
            raise SessionNotFoundError(session_id)
        return runner

    async def remove_session(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def session_count(self) -> int:
        """Used only by tests/observability - never exposed to API clients."""
        async with self._lock:
            return len(self._sessions)
