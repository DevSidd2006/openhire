"""
HTTP error translation.

One rule governs this file: **the client never receives an internal
message.** Every response body is built from pre-written, client-safe text;
the real exception - which can carry prompt fragments, provider errors,
credentials or filesystem paths - is logged server-side and stops there.
That rule predates this chunk and is unchanged.

What this chunk added:

  * a handler for `core.errors.AppError`, so any layer can raise a typed
    error and get correct HTTP behaviour without another bespoke handler
    here (that is the point of the hierarchy - see core/errors.py);
  * a handler for FastAPI's `RequestValidationError`, so a 422 has the same
    `{error, detail}` shape as every other error instead of FastAPI's
    default `{"detail": [...]}`, and so that the rejected *values* are
    stripped from the response;
  * `request_id` on every error body, matching the `X-Request-ID` response
    header, so a client can report an error a server log can be found for.

The pre-existing domain handlers (`SessionNotFoundError`,
`InvalidRequestError`, `InterviewSessionError`) keep their exact status
codes and body shapes, because clients and tests depend on them.
"""
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from api.registry import SessionNotFoundError
from core.context import get_request_id
from core.errors import AppError
from core.logging import get_logger, log_context
from utils.interview_session import InterviewSessionError, SessionStatus

logger = get_logger("api.errors")


def _request_id(request: Request) -> str | None:
    """Prefer the id the middleware stamped on this request; fall back to the
    context variable so an error raised outside the middleware (e.g. during
    lifespan) still carries one if it exists."""
    return getattr(request.state, "request_id", None) or get_request_id()


def _error_body(error: str, detail: str, request: Request, **extra) -> dict:
    body = {"error": error, "detail": detail}
    request_id = _request_id(request)
    if request_id:
        body["request_id"] = request_id
    body.update(extra)
    return body


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


def _sanitised_validation_errors(exc: RequestValidationError) -> list[dict]:
    """Field-level detail for a 422, with the rejected values removed.

    pydantic's `errors()` includes an `input` key holding the value that
    failed validation. For this API that value can be an entire answer
    transcript or resume, so echoing it back would put candidate content
    into an error response (and, from there, into client-side error logs).
    Only the location and the message are returned - which is what a client
    needs to fix the request.
    """
    sanitised = []
    for error in exc.errors():
        sanitised.append(
            {
                "field": ".".join(str(part) for part in error.get("loc", ())),
                "message": error.get("msg", "invalid value"),
                "type": error.get("type", "value_error"),
            }
        )
    return sanitised


def register_exception_handlers(app) -> None:
    @app.exception_handler(SessionNotFoundError)
    async def _not_found(request: Request, exc: SessionNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=_error_body(
                "session_not_found", "No session found for the given session_id", request
            ),
        )

    @app.exception_handler(InvalidRequestError)
    async def _invalid_request(request: Request, exc: InvalidRequestError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_error_body("invalid_request", str(exc), request),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Logged at INFO, not WARNING: a malformed request from a client is
        # normal traffic, not an incident, and logging it louder than that
        # trains operators to ignore the level.
        logger.info(
            "request failed schema validation",
            extra=log_context(event="validation_error", path=request.url.path),
        )
        return JSONResponse(
            status_code=422,
            content=_error_body(
                "validation_error",
                "The request body or parameters did not match the expected schema.",
                request,
                errors=_sanitised_validation_errors(exc),
            ),
        )

    @app.exception_handler(InterviewSessionError)
    async def _session_error(request: Request, exc: InterviewSessionError) -> JSONResponse:
        runner = getattr(request.state, "runner", None)
        status_code = session_error_to_http_status(exc, runner)
        if status_code == 500:
            # Genuine session failure (P5 Phase 18: "log session failure") -
            # real cause logged server-side only, never returned to the client.
            logger.error(
                "session failure: %s",
                exc,
                extra=log_context(event="session_failure", path=request.url.path),
            )
            detail = "The interview session failed and could not continue."
        else:
            logger.warning(
                "invalid session state transition: %s",
                exc,
                extra=log_context(event="invalid_session_state", path=request.url.path),
            )
            detail = "This operation is not valid for the session's current state."
        return JSONResponse(
            status_code=status_code, content=_error_body("session_error", detail, request)
        )

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        """The general handler for the typed error hierarchy.

        `internal_detail` is logged and never serialised; `exc.detail` is the
        client-safe text the exception was constructed with. 5xx is logged
        with a traceback because it indicates a server fault worth
        investigating; 4xx is not, because it is the caller's to fix.
        """
        log = logger.error if exc.status_code >= 500 else logger.warning
        log(
            "%s: %s",
            exc.code,
            exc.internal_detail or exc.detail,
            exc_info=exc.status_code >= 500,
            extra=log_context(
                event="app_error",
                code=exc.code,
                status_code=exc.status_code,
                path=request.url.path,
                **exc.context,
            ),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_body(request_id=_request_id(request)),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Catch-all: never leak a raw exception/stack trace to the client.
        logger.error(
            "unexpected error: %s",
            exc,
            exc_info=True,
            extra=log_context(event="unhandled_error", path=request.url.path),
        )
        return JSONResponse(
            status_code=500,
            content=_error_body("internal_error", "An unexpected error occurred.", request),
        )
