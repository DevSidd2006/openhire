"""
API layer exceptions.

These are defined in a separate module to avoid circular imports - they need
to be imported by both api/registry.py and services/interview_service.py,
but those modules are part of a cycle involving utils/ and agents/.
"""


class SessionNotFoundError(Exception):
    """No session exists for the given session_id - either it was never
    created or was already removed. Distinct from InterviewSessionError
    (utils/interview_session.py), which is about an EXISTING session being
    in the wrong lifecycle state - this is about the session not existing
    in the registry at all (maps to HTTP 404, not 409)."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"No session found for session_id={session_id!r}")


class InvalidRequestError(Exception):
    """A request was well-formed JSON matching the schema (so FastAPI's own
    422 validation passed) but violates a business rule the schema can't
    express - e.g. candidate_id not matching parsed_resume.candidate_id.
    Maps to 400, distinct from schema-level 422.

    Retained as its own class rather than folded into
    `core.errors.BadRequestError`: it is raised from api/routes/*.py and
    asserted on by tests, and both express the same thing. New code in the
    service or repository layers should raise `BadRequestError` instead;
    both produce an identical `invalid_request` 400.
    """
