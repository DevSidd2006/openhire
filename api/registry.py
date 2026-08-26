"""
In-process registry of live interview sessions.

A safe abstraction over "where do running InterviewSessionRunner instances
live between HTTP requests" - deliberately small and swappable rather than a
bare dict scattered across route modules. No database, no Redis: local,
in-process state only.

Why this is NOT the thing the database replaces
-----------------------------------------------
`SessionRegistry` holds live `InterviewSessionRunner` *objects*. Each one
owns an asyncio lock, an LLM client and a per-question idempotency cache; it
is a process object and cannot be stored in a row. So this registry stays
in-process even after the database lands - what moves to the database is the
session's durable *record* and its sealed transcript, which is what
`repositories/interfaces.py` defines (`SessionRepository`,
`TranscriptRepository`).

This class already satisfies the `SessionRuntimeRegistry` protocol in
repositories/interfaces.py structurally, with no change to its methods or
its class hierarchy - which is why `app.state.registry` remains exactly the
seam it always was.
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

    async def restore_session(
        self, session_id: str, runner: InterviewSessionRunner
    ) -> InterviewSessionRunner:
        """Insert a REHYDRATED runner under its ORIGINAL session_id
        (Chunk 3: services/interview_service.py:_restore_runner).

        Distinct from `create_session`, which always mints a brand-new id -
        restoring a session must return to the SAME session_id a client
        already holds, since that id is the only thing the client has to
        keep asking for it with.

        If another concurrent caller already restored (or this session was
        never actually evicted) a runner for this session_id, THAT existing
        runner is returned instead and the freshly-rehydrated `runner`
        argument passed in here is silently discarded. This guarantees
        exactly one live runner object per session_id ever exists at a
        time - `InterviewSessionRunner`'s own concurrency guarantees (its
        internal asyncio.Lock and per-question idempotency cache, P4 Phase
        11/12) only hold for a single instance, so two different runner
        objects both believing they own the same session_id would silently
        reintroduce the exact race those mechanisms exist to prevent.
        """
        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing
            self._sessions[session_id] = runner
            return runner

    async def remove_session(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def session_count(self) -> int:
        """Used only by tests/observability - never exposed to API clients."""
        async with self._lock:
            return len(self._sessions)
