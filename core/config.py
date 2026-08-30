"""
Typed application settings for the backend service.

Relationship to config/settings.py
----------------------------------
`config/settings.py` is the project's long-standing settings module and is
NOT replaced here. Every agent, provider and utility already imports its
module-level constants (LLM_PROVIDER, GROQ_API_KEY, MAX_QUESTIONS_PER_INTERVIEW,
...) and several tests monkeypatch those attributes directly. Rewriting it
would break both contracts for no gain, so it stays exactly as it is and
remains the single source of truth for *domain and provider* configuration.

What was genuinely missing was configuration for the **service** itself:
which environment we are running in, what the CORS policy is, how logs are
formatted, whether authentication is enforced, and how the process is
bound. Those values had no home at all - CORS in particular was simply
absent, which is why api/app.py had to justify serving its own static page
"so no CORS configuration is required". This module is that home.

Two rules keep the two modules from drifting:

1. A value lives in EXACTLY one of them. Nothing is redeclared.
2. Provider/domain values are exposed here only as read-through properties
   that resolve `config.settings` at *access* time. They are never copied
   into a field, so `monkeypatch.setattr("config.settings.GEMINI_API_KEY", ...)`
   keeps working and this module can never serve a stale value.

Settings are read from the process environment (with `.env` already loaded
by config/settings.py) and validated once, at startup, by
`AppSettings.from_env()`. Invalid configuration fails loudly there rather
than surfacing as a confusing error on the first request.

JWT / Authentication Configuration
-----------------------------------
The following settings control JWT token generation and validation:

- jwt_secret_key: Secret key used to sign and verify JWT tokens. Must be
  changed in production environments.
- access_token_expire_minutes: Lifetime of access tokens in minutes (default: 15).
- refresh_token_expire_days: Lifetime of refresh tokens in days (default: 7).
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Importing config.settings here (rather than inside each property) is safe
# and intentional: it is a side-effect-light module that only reads env vars
# and creates the data/outputs directories, and it is already imported by
# virtually every other module in the project.
import config.settings as legacy_settings

Environment = Literal["development", "test", "staging", "production"]

# CORS: the wildcard is only ever an explicit operator choice, never a
# default. `["*"]` combined with credentials is rejected outright below -
# browsers refuse that combination anyway, and silently sending it is how
# teams end up believing credentialed cross-origin requests are working.
_WILDCARD = "*"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_list(name: str, default: Optional[List[str]] = None) -> List[str]:
    """Comma-separated env var -> list. Empty entries are dropped so that a
    trailing comma or a blank value can never become an empty-string origin
    (which would silently never match anything)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


class AppSettings(BaseModel):
    """Validated, immutable service configuration.

    Frozen on purpose: configuration is read once at startup and must not be
    mutated by request handlers. Tests that need different values build a
    new instance (or call `reset_settings_cache()`), which keeps the
    "settings are constant for the lifetime of the process" guarantee honest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # -- identity / environment ------------------------------------------
    app_name: str = "OpenHire Live Interview API"
    app_version: str = "0.1.0"
    environment: Environment = "development"
    debug: bool = False

    # -- HTTP ------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    # Empty by default: the existing routes are mounted at "/sessions",
    # "/health" and "/ws/sessions/{id}", and the frontend and the shipped
    # voice client (pages/voice-interview.html) already call those paths.
    # Introducing a "/api/v1" prefix now would break both, so the prefix is
    # opt-in via API_PREFIX and defaults to no change at all.
    api_prefix: str = ""
    serve_static_pages: bool = True

    # -- CORS ------------------------------------------------------------
    cors_allow_origins: List[str] = Field(default_factory=list)
    cors_allow_origin_regex: Optional[str] = None
    cors_allow_credentials: bool = False
    cors_allow_methods: List[str] = Field(default_factory=lambda: ["*"])
    cors_allow_headers: List[str] = Field(default_factory=lambda: ["*"])
    cors_max_age: int = Field(default=600, ge=0)

    # -- logging ---------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False
    log_access: bool = True
    # Request/response bodies are never logged. This only controls whether
    # the query string is kept on the access line, since query strings are a
    # common place for tokens to leak into log files.
    log_query_string: bool = False

    # -- auth boundary (see core/security.py) -----------------------------
    # Chunk 1 establishes the boundary only. `auth_enabled=False` means every
    # request is served as the anonymous principal, which is exactly the
    # behaviour the current session APIs already have - no endpoint changes
    # semantics until a real AuthProvider is installed in a later chunk.
    auth_enabled: bool = False
    auth_required_by_default: bool = False

    # -- limits ----------------------------------------------------------
    max_request_body_bytes: int = Field(default=12 * 1024 * 1024, ge=1024)

    # -- JWT / Authentication -----------------------------------------------
    jwt_secret_key: str = Field(default="dev-secret-key-change-in-production")
    access_token_expire_minutes: int = Field(default=15, ge=1)
    refresh_token_expire_days: int = Field(default=7, ge=1)

    # -- persistence -------------------------------------------------------
    # Database chunk: a PostgreSQL DSN (e.g.
    # "postgresql://user:pass@host:5432/openhire"). Empty (the default)
    # means "no database configured" - build_default_container falls back
    # to repositories/memory.py's in-process stubs exactly as it always
    # has, so leaving this unset changes nothing about existing behaviour.
    # Never logged - see public_summary(), which reports only whether
    # persistence is durable, never the DSN itself.
    database_url: str = ""

    # ------------------------------------------------------------------
    # Read-through views of config/settings.py (never copies - see docstring)
    # ------------------------------------------------------------------
    @property
    def llm_provider(self) -> str:
        return legacy_settings.LLM_PROVIDER

    @property
    def audio_provider(self) -> str:
        return legacy_settings.AUDIO_PROVIDER

    @property
    def tts_provider(self) -> str:
        return legacy_settings.TTS_PROVIDER

    @property
    def vector_store_type(self) -> str:
        return legacy_settings.VECTOR_STORE_TYPE

    @property
    def embedding_provider(self) -> str:
        return legacy_settings.EMBEDDING_PROVIDER

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @field_validator("api_prefix")
    @classmethod
    def _normalise_prefix(cls, value: str) -> str:
        value = (value or "").strip().rstrip("/")
        if value and not value.startswith("/"):
            value = "/" + value
        return value

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        value = (value or "INFO").strip().upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if value not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}, got {value!r}")
        return value

    def model_post_init(self, __context) -> None:
        # A wildcard origin cannot be combined with credentials: the browser
        # rejects `Access-Control-Allow-Origin: *` on a credentialed request,
        # so shipping that pair means cross-origin auth is quietly broken.
        if self.cors_allow_credentials and _WILDCARD in self.cors_allow_origins:
            raise ValueError(
                "CORS_ALLOW_CREDENTIALS=true cannot be combined with "
                "CORS_ALLOW_ORIGINS=* - list the exact origins instead."
            )
        if self.is_production:
            if _WILDCARD in self.cors_allow_origins:
                raise ValueError(
                    "CORS_ALLOW_ORIGINS=* is not permitted when ENVIRONMENT=production - "
                    "list the exact frontend origins."
                )
            if self.debug:
                raise ValueError("DEBUG=true is not permitted when ENVIRONMENT=production.")

    # ------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def docs_url(self) -> Optional[str]:
        """OpenAPI docs are served everywhere except production, where the
        schema of an internal hiring API is not something to publish
        unauthenticated."""
        return None if self.is_production else "/docs"

    @property
    def openapi_url(self) -> Optional[str]:
        return None if self.is_production else "/openapi.json"

    def public_summary(self) -> dict:
        """A credential-free description of how the service is configured,
        safe to log at startup and to return from /health.

        Only provider *names* - never keys, connection strings or file paths.
        """
        return {
            "app": self.app_name,
            "version": self.app_version,
            "environment": self.environment,
            "debug": self.debug,
            "llm_provider": self.llm_provider,
            "audio_provider": self.audio_provider,
            "tts_provider": self.tts_provider,
            "auth_enabled": self.auth_enabled,
        }

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> "AppSettings":
        """Build settings from the process environment.

        `config.settings` has already called `load_dotenv()` at import time
        (and `load_dotenv` does not override variables already present in the
        environment), so a value set by the deployment platform always wins
        over `.env` - the ordering a deployed service needs.
        """
        environment = _env("ENVIRONMENT", "development").strip().lower() or "development"
        return cls(
            app_name=_env("APP_NAME", "OpenHire Live Interview API"),
            app_version=_env("APP_VERSION", "0.1.0"),
            environment=environment,  # type: ignore[arg-type]
            debug=_env_bool("DEBUG", False),
            host=_env("HOST", "0.0.0.0"),
            port=_env_int("PORT", 8000),
            api_prefix=_env("API_PREFIX", ""),
            serve_static_pages=_env_bool("SERVE_STATIC_PAGES", True),
            cors_allow_origins=_env_list("CORS_ALLOW_ORIGINS"),
            cors_allow_origin_regex=_env("CORS_ALLOW_ORIGIN_REGEX") or None,
            cors_allow_credentials=_env_bool("CORS_ALLOW_CREDENTIALS", False),
            cors_allow_methods=_env_list("CORS_ALLOW_METHODS", ["*"]),
            cors_allow_headers=_env_list("CORS_ALLOW_HEADERS", ["*"]),
            cors_max_age=_env_int("CORS_MAX_AGE", 600),
            log_level=_env("LOG_LEVEL", "DEBUG" if _env_bool("DEBUG", False) else "INFO"),
            log_json=_env_bool("LOG_JSON", False),
            log_access=_env_bool("LOG_ACCESS", True),
            log_query_string=_env_bool("LOG_QUERY_STRING", False),
            auth_enabled=_env_bool("AUTH_ENABLED", False),
            auth_required_by_default=_env_bool("AUTH_REQUIRED_BY_DEFAULT", False),
            max_request_body_bytes=_env_int("MAX_REQUEST_BODY_BYTES", 12 * 1024 * 1024),
            jwt_secret_key=_env("JWT_SECRET_KEY", "dev-secret-key-change-in-production"),
            access_token_expire_minutes=_env_int("ACCESS_TOKEN_EXPIRE_MINUTES", 15),
            refresh_token_expire_days=_env_int("REFRESH_TOKEN_EXPIRE_DAYS", 7),
            database_url=_env("DATABASE_URL", ""),
        )


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Process-wide settings, resolved once.

    Cached so that every dependency, middleware and log record sees the same
    object, and so that a malformed environment fails at the first call
    rather than intermittently.
    """
    return AppSettings.from_env()


def reset_settings_cache() -> None:
    """Drop the cached settings so the next `get_settings()` re-reads the
    environment. For tests and for a deliberate in-process reconfiguration
    only - never call this from request-handling code."""
    get_settings.cache_clear()
