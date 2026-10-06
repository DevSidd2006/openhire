"""
Canonical application error hierarchy.

Before this module, the backend had exactly two error types with an HTTP
meaning - `api.registry.SessionNotFoundError` and
`utils.interview_session.InterviewSessionError` - and every new failure mode
would have had to either reuse one of them inaccurately or add another
one-off handler in api/errors.py. Neither scales past the interview
endpoints.

`AppError` is the shared base: an exception that already knows the HTTP
status it deserves and the stable, machine-readable `code` clients switch
on. api/errors.py registers a single handler for it, so a new failure mode
in any future layer (repositories, auth, services) gets correct HTTP
behaviour by choosing the right exception class instead of by writing
transport code.

Two rules this module exists to enforce:

1. **The client never sees an internal message.** `detail` is written for
   the caller; anything diagnostic goes in `internal_detail`, which is
   logged server-side and never serialised. This mirrors the discipline
   api/errors.py already applied by hand.
2. **The existing domain exceptions keep their meaning.** `SessionNotFoundError`
   and `InterviewSessionError` are deliberately NOT re-parented onto
   `AppError`: they are domain exceptions raised by code that must stay
   framework-free, and their existing handlers (which map an
   InterviewSessionError to 409 *or* 500 depending on the runner's status)
   continue to own their translation.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class AppError(Exception):
    """Base class for errors that carry their own HTTP semantics.

    Subclasses set `status_code` and `code`; callers may override `detail`
    (client-safe) and attach `internal_detail` (server-only) plus arbitrary
    non-sensitive `context` for structured logging.
    """

    status_code: int = 500
    code: str = "internal_error"
    detail: str = "An unexpected error occurred."

    def __init__(
        self,
        detail: Optional[str] = None,
        *,
        internal_detail: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.detail = detail or type(self).detail
        # What actually went wrong: provider errors, upstream messages,
        # identifiers. Logged, never returned.
        self.internal_detail = internal_detail
        self.context: Dict[str, Any] = context or {}
        super().__init__(self.internal_detail or self.detail)

    def to_body(self, *, request_id: Optional[str] = None) -> Dict[str, Any]:
        """The wire representation. Matches `api.models.ErrorResponse`
        (`error` + `detail`) so existing clients and tests are unaffected;
        `request_id` is additive and only present when one exists."""
        body: Dict[str, Any] = {"error": self.code, "detail": self.detail}
        if request_id:
            body["request_id"] = request_id
        return body


# ---------------------------------------------------------------------------
# 4xx - the caller can fix these
# ---------------------------------------------------------------------------

class BadRequestError(AppError):
    """Well-formed for the schema, but violates a business rule the schema
    cannot express. 400, distinct from FastAPI's schema-level 422."""

    status_code = 400
    code = "invalid_request"
    detail = "The request was not valid."


class UnauthorizedError(AppError):
    """No usable credential was presented. 401.

    Unused while `AUTH_ENABLED=false` - defined now so that the auth chunk
    adds a credential verifier, not a new error vocabulary."""

    status_code = 401
    code = "unauthorized"
    detail = "Authentication is required for this operation."


class ForbiddenError(AppError):
    """Authenticated, but not permitted. 403.

    Kept strictly separate from `NotFoundError`: for a *recruiter* resource
    the two are interchangeable to an attacker probing for existence, and
    the choice of which to return is a deliberate decision for the endpoint
    that owns the resource - not something to bake in here."""

    status_code = 403
    code = "forbidden"
    detail = "You do not have permission to perform this operation."


class NotFoundError(AppError):
    """The addressed resource does not exist. 404."""

    status_code = 404
    code = "not_found"
    detail = "The requested resource was not found."


class ConflictError(AppError):
    """The resource exists but its current state does not allow this
    operation (the HTTP shape of "wrong lifecycle state"). 409."""

    status_code = 409
    code = "conflict"
    detail = "This operation is not valid for the resource's current state."


class PayloadTooLargeError(AppError):
    """413 - the request body exceeded `MAX_REQUEST_BODY_BYTES`."""

    status_code = 413
    code = "payload_too_large"
    detail = "The request body is larger than this endpoint accepts."


# ---------------------------------------------------------------------------
# 5xx - the caller cannot fix these
# ---------------------------------------------------------------------------

class InternalError(AppError):
    """500 - an unexpected server-side failure."""

    status_code = 500
    code = "internal_error"
    detail = "An unexpected error occurred."


class ConfigurationError(AppError):
    """The service is misconfigured (a provider selected with no credential,
    an unparseable setting). Surfaces as 500 because it is an operator
    failure, never the caller's - and is raised at startup wherever it can
    be detected there instead."""

    status_code = 500
    code = "configuration_error"
    detail = "The service is not correctly configured."


class DependencyError(AppError):
    """An upstream dependency (LLM provider, speech service, database)
    failed or was unavailable. 503, so that a caller and a load balancer can
    distinguish "try again" from a genuine 500."""

    status_code = 503
    code = "dependency_unavailable"
    detail = "A required upstream service is currently unavailable."


class NotImplementedYetError(AppError):
    """A capability whose interface exists but whose implementation is owned
    by a later chunk (notably: every persistence method that needs the real
    database). 501.

    Raised rather than returning a plausible-looking empty result, so that a
    missing implementation can never be mistaken for real data - the same
    principle the interview engine already follows by refusing to fabricate
    a score."""

    status_code = 501
    code = "not_implemented"
    detail = "This capability is not available yet."


__all__ = [
    "AppError",
    "BadRequestError",
    "UnauthorizedError",
    "ForbiddenError",
    "NotFoundError",
    "ConflictError",
    "PayloadTooLargeError",
    "InternalError",
    "ConfigurationError",
    "DependencyError",
    "NotImplementedYetError",
]
