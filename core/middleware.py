"""
HTTP middleware.

Only middleware the backend actually needs is installed - each one below
exists because something concrete was missing, not to fill out a template.

  RequestContextMiddleware  There was no way to correlate the several log
                            lines one request produces, and no way to tie a
                            client-reported error back to a server log. It
                            assigns (or accepts) a request id, binds it to
                            the logging context, times the request, and
                            returns it in a response header.

  BodySizeLimitMiddleware   The voice endpoints accept base64 audio in a
                            JSON body. `MAX_UTTERANCE_BYTES` is enforced
                            inside utils/voice_turn.py - but only *after*
                            the whole body has been read and decoded, so an
                            oversized request is already fully buffered in
                            memory by then. This rejects it at the edge.
                            The domain-level limit stays exactly as it is;
                            this is a cheaper outer bound, not a replacement.

  CORSMiddleware            Starlette's own, configured from AppSettings
                            (see `install_middleware`).

Order matters and is asserted by the installation order in
`install_middleware`. Starlette applies middleware outermost-first in
reverse of registration, so RequestContextMiddleware is registered last to
end up outermost - it must be able to log and stamp a request id on
responses produced by everything inside it, including CORS preflights and
body-size rejections.
"""
from __future__ import annotations

import time
import uuid
from typing import Callable

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from core.config import AppSettings
from core.context import REQUEST_ID_HEADER, reset_request_id, set_request_id
from core.errors import PayloadTooLargeError
from core.logging import get_logger, log_context

logger = get_logger("api.access")

# An inbound request id is echoed rather than replaced so a trace survives a
# gateway hop - but it is length-capped and character-filtered first, because
# it goes into every log line for that request and an unbounded,
# newline-containing header value is a log-injection vector.
_MAX_REQUEST_ID_LEN = 64


def _clean_request_id(raw: str | None) -> str:
    if not raw:
        return uuid.uuid4().hex
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in "-_").strip()
    if not cleaned:
        return uuid.uuid4().hex
    return cleaned[:_MAX_REQUEST_ID_LEN]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Correlation id, timing, and the access log."""

    def __init__(self, app, settings: AppSettings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = _clean_request_id(request.headers.get(REQUEST_ID_HEADER))
        token = set_request_id(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # The exception handlers turn this into a response; log the
            # timing here so a failed request still produces an access line
            # with the same shape as a successful one.
            duration_ms = (time.perf_counter() - started) * 1000
            self._log(request, status_code=500, duration_ms=duration_ms)
            reset_request_id(token)
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        # Surfaced deliberately: it is the number a frontend engineer needs
        # when an interview turn feels slow, and it reveals nothing about
        # the candidate or the model.
        response.headers["X-Response-Time-ms"] = f"{duration_ms:.1f}"
        self._log(request, status_code=response.status_code, duration_ms=duration_ms)
        reset_request_id(token)
        return response

    def _log(self, request: Request, *, status_code: int, duration_ms: float) -> None:
        if not self._settings.log_access:
            return
        principal = getattr(request.state, "principal", None)
        # `request.url.path` only - never the query string unless explicitly
        # enabled, and never headers or body. Path parameters such as
        # session_id are already opaque ids, not personal data.
        logger.info(
            "%s %s -> %s",
            request.method,
            request.url.path,
            status_code,
            extra=log_context(
                event="http_request",
                method=request.method,
                path=request.url.path,
                query=request.url.query if self._settings.log_query_string else None,
                status_code=status_code,
                duration_ms=round(duration_ms, 1),
                principal_type=(
                    principal.principal_type.value if principal is not None else None
                ),
            ),
        )


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized bodies before they are buffered.

    Uses `Content-Length` only. A chunked request without that header is let
    through rather than being read here to measure it: consuming the stream
    in middleware would break the downstream handler's ability to read it,
    and the domain-level `MAX_UTTERANCE_BYTES` check in utils/voice_turn.py
    still bounds the audio that actually matters. This is defence in depth,
    not the sole limit.
    """

    def __init__(self, app, max_bytes: int) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                length = int(raw_length)
            except ValueError:
                length = 0
            if length > self._max_bytes:
                error = PayloadTooLargeError()
                logger.warning(
                    "rejected oversized request body",
                    extra=log_context(
                        event="body_too_large",
                        path=request.url.path,
                        content_length=length,
                        limit=self._max_bytes,
                    ),
                )
                return JSONResponse(
                    status_code=error.status_code,
                    content=error.to_body(request_id=getattr(request.state, "request_id", None)),
                )
        return await call_next(request)


def install_middleware(app: FastAPI, settings: AppSettings) -> None:
    """Install the middleware stack, innermost first.

    Starlette runs middleware in reverse registration order, so the last one
    added here is the outermost at request time. That places
    RequestContextMiddleware outside everything, which is required for the
    request id to appear on CORS-preflight and body-limit responses too.
    """
    if settings.cors_allow_origins or settings.cors_allow_origin_regex:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_origin_regex=settings.cors_allow_origin_regex,
            allow_credentials=settings.cors_allow_credentials,
            allow_methods=settings.cors_allow_methods,
            allow_headers=settings.cors_allow_headers,
            # Without exposing it, browser JS cannot read the request id off
            # a cross-origin response and so cannot report it in a bug.
            expose_headers=[REQUEST_ID_HEADER, "X-Response-Time-ms"],
            max_age=settings.cors_max_age,
        )
    else:
        # No CORS middleware at all rather than a permissive default. The
        # API and the shipped voice client (pages/) are same-origin today,
        # so cross-origin access is something an operator opts into by
        # setting CORS_ALLOW_ORIGINS - it is never on by accident.
        logger.info(
            "CORS is disabled (no CORS_ALLOW_ORIGINS configured); "
            "same-origin requests are unaffected."
        )

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(RequestContextMiddleware, settings=settings)


__all__ = [
    "BodySizeLimitMiddleware",
    "RequestContextMiddleware",
    "install_middleware",
]
