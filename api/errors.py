"""
P5 Phase 8: HTTP error model.

Maps the existing domain exceptions (api/registry.SessionNotFoundError,
utils.interview_session.InterviewSessionError) to clean, consistent HTTP
responses. Never forwards a raw exception message that could contain
internal details (prompts, provider errors, file paths) straight to the
client - only pre-written, client-safe text. The real exception is always
logged server-side first.
"""
from fastapi import HTTPException
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from api.registry import SessionNotFoundError
from utils.interview_session import InterviewSessionError, SessionStatus
from utils.logging import get_logger

logger = get_logger("api.errors")


def _error_body(error: str, detail: str) -> dict:
    return {"error": error, "detail": detail}


class InvalidRequestError(Exception):
    """A request was well-formed JSON matching the schema (so FastAPI's own
    422 validation passed) but violates a business rule the schema can't
    express - e.g. candidate_id not matching parsed_resume.candidate_id.
    Maps to 400, distinct from schema-level 422."""


def session_error_to_http_status(exc: InterviewSessionError, runner) -> int:
    """An InterviewSessionError covers two different situations that
    deserve different status codes:
      - the caller asked for an operation the session's current lifecycle
        doesn't allow (submit after sealed, start twice, ...) -> 409
      - the session itself failed internally (evaluation/generation
        exhausted retries) -> 500, since that's an operational failure, not
        a client mistake, even though it surfaces through the same
        exception type (utils/interview_session.py deliberately does not
        split this into two exception classes - see its docstring - so the
        distinction is made here, at the transport boundary, using the
        runner's own status rather than string-matching the message).
    """
    if runner is not None and getattr(runner, "status", None) == SessionStatus.FAILED:
        return 500
    return 409


def register_exception_handlers(app) -> None:
    @app.exception_handler(SessionNotFoundError)
    async def _not_found(request: Request, exc: SessionNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=_error_body("session_not_found", f"No session found for the given session_id"),
        )

    @app.exception_handler(InvalidRequestError)
    async def _invalid_request(request: Request, exc: InvalidRequestError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_error_body("invalid_request", str(exc)),
        )

    @app.exception_handler(InterviewSessionError)
    async def _session_error(request: Request, exc: InterviewSessionError) -> JSONResponse:
        runner = getattr(request.state, "runner", None)
        status_code = session_error_to_http_status(exc, runner)
        if status_code == 500:
            # Genuine session failure (P5 Phase 18: "log session failure") -
            # real cause logged server-side only, never returned to the client.
            logger.error(f"session failure on {request.url.path}: {exc}")
            detail = "The interview session failed and could not continue."
        else:
            logger.warning(f"invalid session state transition on {request.url.path}: {exc}")
            detail = "This operation is not valid for the session's current state."
        return JSONResponse(status_code=status_code, content=_error_body("session_error", detail))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Catch-all: never leak a raw exception/stack trace to the client.
        logger.error(f"Unexpected error on {request.url.path}: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content=_error_body("internal_error", "An unexpected error occurred."),
        )
