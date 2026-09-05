"""
Per-request context.

A single `ContextVar` carrying the current request's correlation id. It
lives in its own module (rather than in core/logging.py or
core/middleware.py) so that the logging filter, the middleware and the
error handlers can all reach it without importing each other.

`ContextVar` - not a module-level global and not thread-local - is what
makes this safe under asyncio: each request task gets its own value, so two
concurrent interview requests can never read each other's id.
"""
from contextvars import ContextVar
from typing import Optional

REQUEST_ID_HEADER = "X-Request-ID"

_request_id: ContextVar[Optional[str]] = ContextVar("openhire_request_id", default=None)


def get_request_id() -> Optional[str]:
    """The correlation id of the request currently being handled, or None
    outside of a request (CLI runs, background work, tests)."""
    return _request_id.get()


def set_request_id(request_id: Optional[str]):
    """Bind a correlation id to the current context. Returns the token
    required to reset it (see contextvars.ContextVar.reset)."""
    return _request_id.set(request_id)


def reset_request_id(token) -> None:
    try:
        _request_id.reset(token)
    except ValueError:
        # The token belongs to a different context (can happen if a task was
        # cancelled between set and reset). Nothing to unwind.
        pass
