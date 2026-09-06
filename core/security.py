"""
Authentication and authorisation boundary.

Chunk 1 deliberately implements the *boundary*, not an auth system. A real
implementation needs users, roles, recruiter/organisation ownership and
token or link records - all of which are database schema owned by another
teammate. Building it now would mean inventing that schema, which this
chunk is explicitly not allowed to do.

What is here instead is everything that does NOT depend on the schema:

  * `Principal` - who is making the request, in a form the rest of the
    backend can already depend on.
  * `AuthProvider` - the one interface a real implementation must satisfy.
  * `AnonymousAuthProvider` - the default, which authenticates nobody and
    grants nothing.
  * `JWTAuthProvider` - validates JWT access tokens and returns the Principal.
  * `get_principal` / `require_authenticated` / `require_scopes` - the
    FastAPI dependencies endpoints attach.

Behaviour today, and why nothing breaks
---------------------------------------
`AUTH_ENABLED` defaults to false. In that mode `get_principal` returns the
anonymous principal and `require_authenticated` lets it through, so every
existing session endpoint behaves exactly as it does now - the current
candidate-facing session API is unauthenticated by design (the session_id
itself is the 122-bit capability, see api/registry.py) and Chunk 1 does not
change that.

Turning `AUTH_ENABLED=true` on with only `AnonymousAuthProvider` installed
is a configuration error, not a silent no-op: the service refuses to start
(core/lifespan.py). "Auth is on" must never be a claim the service cannot
back up.

Design notes worth keeping
--------------------------
`require_scopes` compares against a scope set on the principal rather than
a single role string. Roles are a database concept and belong to whoever
owns that table; scopes are a property of the request's authorisation and
can be derived from whatever role model eventually exists, so endpoints
written against scopes will not need rewriting when roles arrive.

The credential is read from the standard `Authorization` header and is
never logged, never placed on `request.state` in raw form, and never
returned in an error body. The dependency yields a `Principal`, not a
token, so a handler cannot accidentally forward one.
"""
from __future__ import annotations

from enum import Enum
from typing import FrozenSet, Optional, Protocol, Sequence, runtime_checkable

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from core.config import AppSettings, get_settings
from core.errors import ForbiddenError, UnauthorizedError


class PrincipalType(str, Enum):
    """Who a request is acting as.

    Four kinds the product has: the recruiter using the dashboard, the
    candidate following an interview link, the service itself (a background
    evaluation worker calling internally), and - as of the admin console -
    the operator account that manages users/content/impersonation. ANONYMOUS
    is the fifth, meaning "no credential was presented".
    """

    ANONYMOUS = "anonymous"
    CANDIDATE = "candidate"
    RECRUITER = "recruiter"
    SERVICE = "service"
    ADMIN = "admin"


class Principal(BaseModel):
    """The authenticated (or unauthenticated) identity behind one request.

    Frozen: a request handler must not be able to widen its own caller's
    authorisation partway through handling.

    Carries no credential - only the identity and what it may do. Whatever
    token, cookie or link secret produced it stays inside the AuthProvider.
    """

    model_config = ConfigDict(frozen=True)

    principal_type: PrincipalType = PrincipalType.ANONYMOUS
    subject_id: Optional[str] = None
    # Set when the principal is scoped to one interview session - the shape a
    # candidate interview link will take. Endpoints can use it to check that
    # the caller is operating on their own session rather than merely on a
    # session_id they happen to hold.
    session_id: Optional[str] = None
    scopes: FrozenSet[str] = Field(default_factory=frozenset)

    @property
    def is_authenticated(self) -> bool:
        return self.principal_type is not PrincipalType.ANONYMOUS

    def has_scopes(self, required: Sequence[str]) -> bool:
        return set(required).issubset(self.scopes)

    def log_fields(self) -> dict:
        """Identity fields safe to attach to a log record."""
        return {
            "principal_type": self.principal_type.value,
            "subject_id": self.subject_id,
        }


ANONYMOUS = Principal()


@runtime_checkable
class AuthProvider(Protocol):
    """The single seam a real authentication implementation plugs into.

    One method. It receives the request (so an implementation is free to
    read a header, a cookie or a path parameter such as an interview link
    token) and returns the `Principal` it represents.

    Contract:
      * Return `ANONYMOUS` when no credential is presented at all.
      * Raise `UnauthorizedError` when a credential IS presented but is
        invalid, expired or malformed. The distinction matters: a missing
        credential on a public endpoint is fine, whereas a bad credential is
        always a 401 and must never be silently downgraded to anonymous.
      * Never raise for authorisation; that is `require_scopes`' job.
    """

    async def authenticate(self, request: Request) -> Principal: ...


class AnonymousAuthProvider:
    """The default provider: authenticates nobody.

    Not a stub that pretends - it genuinely returns the anonymous principal
    for every request, which is an accurate description of the service's
    current security posture. It is rejected at startup when
    `AUTH_ENABLED=true` precisely so that this honesty cannot be mistaken
    for working authentication.
    """

    async def authenticate(self, request: Request) -> Principal:  # noqa: ARG002
        return ANONYMOUS


class JWTAuthProvider:
    """Authentication provider using JWT access tokens.

    Validates JWT tokens from the Authorization header and returns the
    Principal they represent. Uses AuthService.verify_access_token() to
    decode and validate tokens, which handles JWT verification, claims
    extraction, and expiration checking.

    Token format:
      Authorization: Bearer <access_token>
    """

    def __init__(self, auth_service) -> None:
        """Initialize with an AuthService instance."""
        self._auth_service = auth_service

    async def authenticate(self, request: Request) -> Principal:
        """Extract and validate JWT token from Authorization header.

        Returns the Principal if token is valid. Returns ANONYMOUS if no
        token is presented. Raises UnauthorizedError if a token IS presented
        but is invalid/expired.
        """
        # Read Authorization header
        auth_header = request.headers.get("Authorization", "").strip()
        if not auth_header:
            return ANONYMOUS

        # Parse "Bearer <token>" format
        try:
            scheme, _, token = auth_header.partition(" ")
            if scheme.lower() != "bearer" or not token:
                return ANONYMOUS
        except ValueError:
            return ANONYMOUS

        # Validate token and return Principal
        return await self._auth_service.verify_access_token(token)


def get_auth_provider(request: Request) -> AuthProvider:
    """Resolve the installed provider from the application container.

    Falls back to `AnonymousAuthProvider` if the container is absent, which
    happens only when an app is constructed outside the normal lifespan
    (some unit tests). Failing closed is not an option here - there is
    nothing to fail closed *to* until a real provider exists - so it fails
    to the same posture the service already has, and startup validation
    covers the deployed case.
    """
    container = getattr(request.app.state, "container", None)
    provider = getattr(container, "auth_provider", None)
    return provider or AnonymousAuthProvider()


async def get_principal(
    request: Request,
    provider: AuthProvider = Depends(get_auth_provider),
    settings: AppSettings = Depends(get_settings),
) -> Principal:
    """The identity behind this request. Attach to any endpoint that needs
    to know who is calling.

    With `AUTH_ENABLED=false` this short-circuits to `ANONYMOUS` without
    consulting the provider at all, so no endpoint changes behaviour while
    the boundary is inert.

    The resolved principal is stashed on `request.state` for the access-log
    middleware; only `principal_type`/`subject_id` are ever read from it.
    """
    if not settings.auth_enabled:
        request.state.principal = ANONYMOUS
        return ANONYMOUS

    principal = await provider.authenticate(request)
    request.state.principal = principal
    return principal


async def require_authenticated(
    principal: Principal = Depends(get_principal),
    settings: AppSettings = Depends(get_settings),
) -> Principal:
    """Endpoint dependency: reject anonymous callers.

    Inert while `AUTH_ENABLED=false`, so it can be attached to endpoints now
    and start enforcing the moment a provider is installed - which is the
    point of adding it in this chunk rather than the next.
    """
    if not settings.auth_enabled:
        return principal
    if not principal.is_authenticated:
        raise UnauthorizedError()
    return principal


def require_scopes(*required: str):
    """Build an endpoint dependency requiring every named scope.

    Returns a dependency rather than being one, so an endpoint reads
    `Depends(require_scopes("interview:read"))` and the requirement is
    visible in the route definition and in the generated OpenAPI schema.
    """

    async def _dependency(
        principal: Principal = Depends(require_authenticated),
        settings: AppSettings = Depends(get_settings),
    ) -> Principal:
        if not settings.auth_enabled:
            return principal
        if not principal.has_scopes(required):
            # The missing scope names are NOT returned to the caller: they
            # describe the server's permission model and telling an
            # unauthorised caller exactly which permission to obtain is a
            # gift to anyone probing it. They go to the log instead.
            raise ForbiddenError(
                internal_detail=f"missing required scopes: {sorted(set(required) - principal.scopes)}",
                context={"principal_type": principal.principal_type.value},
            )
        return principal

    return _dependency


__all__ = [
    "ANONYMOUS",
    "AnonymousAuthProvider",
    "AuthProvider",
    "JWTAuthProvider",
    "Principal",
    "PrincipalType",
    "get_auth_provider",
    "get_principal",
    "require_authenticated",
    "require_scopes",
]
