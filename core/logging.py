"""
Backend logging configuration.

`utils/logging.py` is unchanged and still owns the *project's* logging
helpers (`setup_logging`, `get_logger`) - agents, the pipeline and `main.py`
all use it. What it does not do, and was never meant to do, is configure
logging for a long-running web service: it attaches handlers to one named
logger at a time, always writes a file under `outputs/logs/`, and has no
concept of a request correlation id.

This module configures the **root** logger once, at application startup, so
that every library logger (uvicorn, httpx, our own `api.*` / `core.*` /
`agent.*` loggers) is formatted identically and carries the request id of
whichever request produced it. `utils.logging.get_logger` keeps working
unchanged - it returns a plain `logging.Logger`, which now simply
propagates to a properly configured root.

Nothing here ever logs a request body, a response body, an answer
transcript, or a credential. Interview content is candidate data; the API
logs identifiers and outcomes only, exactly as api/routes/interview.py
already does.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Optional

from core.config import AppSettings
from core.context import get_request_id

# Loggers that are noisy at INFO and whose information we already emit
# ourselves through the access log.
_NOISY_LOGGERS = {
    "uvicorn.access": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "multipart": logging.WARNING,
}

_CONFIGURED = False


class RequestIdFilter(logging.Filter):
    """Attaches the current request's correlation id to every record.

    A filter rather than a custom Logger/Adapter, because it has to apply to
    records emitted by third-party libraries too - they will never call our
    adapter, but they do pass through the root handler's filters.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True


class ConsoleFormatter(logging.Formatter):
    """Human-readable single line, request id included."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log aggregation in a deployed
    environment (LOG_JSON=true).

    `default=str` on the exception/extra payload keeps a stray non-JSON
    value from turning a log write into a second exception - a logging path
    that can itself raise is worse than a slightly lossy log line.
    """

    _RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "asctime", "message", "request_id", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(settings: AppSettings, *, force: bool = False) -> None:
    """Configure the root logger for the service. Idempotent.

    Called from the application lifespan (core/lifespan.py) rather than at
    import time: importing a module must never reconfigure the host
    process's logging, which would corrupt pytest's own capture and any
    embedding application.
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # Windows consoles default to cp1252 and raise UnicodeEncodeError on the
    # non-ASCII characters this project logs. Same reasoning as
    # utils/logging.py, applied once at the root instead of per-logger.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.log_json else ConsoleFormatter())
    handler.addFilter(RequestIdFilter())

    root.addHandler(handler)
    root.setLevel(settings.log_level)

    for name, level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(level)

    _CONFIGURED = True


def reset_logging() -> None:
    """Allow a subsequent `configure_logging` to take effect. Test helper."""
    global _CONFIGURED
    _CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    """Service-layer logger accessor.

    Identical in behaviour to `utils.logging.get_logger` (both return
    `logging.getLogger(name)`); re-exported here so that modules in `core/`
    do not have to reach into `utils/` for a one-line helper, and so the
    service layer has a single obvious import.
    """
    return logging.getLogger(name)


def log_context(**fields) -> dict:
    """Build the `extra=` payload for a structured log call, dropping keys
    whose value is None so optional identifiers do not clutter every line.

    Use for identifiers and outcomes only. Never pass answer text, question
    text, audio, prompts or credentials through here.
    """
    return {key: value for key, value in fields.items() if value is not None}


def redact(value: Optional[str], *, keep: int = 4) -> str:
    """Render a secret safely if one ever has to appear in a diagnostic.

    Returns the last `keep` characters only. Preferred over logging nothing
    at all when an operator needs to confirm *which* key is loaded, and
    strictly better than the alternative of someone temporarily printing the
    raw value during an incident.
    """
    if not value:
        return "<unset>"
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]
