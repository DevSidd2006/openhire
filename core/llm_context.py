"""The ContextVar seam BYOK uses to reach get_llm_provider() (providers/llm/__init__.py)
without threading a provider through every agent constructor and every
core/container.py factory.

Why a ContextVar and not wider plumbing
----------------------------------------
`agents/base.py:BaseAgent.__init__` resolves its provider as
`llm_provider or get_llm_provider()`, called deep inside a tree of agents
built by core/container.py's `*_factory_for` methods. Threading a user_id
or a provider through every one of those constructors for one optional
BYOK value would be a wide diff across every production code path whose
only purpose is carrying that one value. A ContextVar touches exactly the
resolution seam inside get_llm_provider() and nothing else.

What this does NOT solve (see docs/superpowers/specs/2026-09-06-byok-design.md
"The boundary, stated plainly")
------------------------------------------------------------------------------
Ambient context only reaches code running inside the request/task that set
it. `main.py` (the CLI) and any future out-of-process worker see no
ContextVar and correctly fall through to the environment. A long-lived
interview session that outlives the request that created it must NOT rely
on this - it stores the owning recruiter's user_id and re-resolves
explicitly at use time instead (services/evaluation_service.py).
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from providers.base import LLMProvider

_current_llm_provider: ContextVar[Optional["LLMProvider"]] = ContextVar(
    "_current_llm_provider", default=None
)


def set_context_provider(
    provider: Optional["LLMProvider"],
) -> Token[Optional["LLMProvider"]]:
    """Set the provider for the current context (request). Returns a Token
    that MUST be passed to reset_context_provider when the request ends -
    see api/dependencies for the yield-dependency that does this."""
    return _current_llm_provider.set(provider)


def reset_context_provider(token: Token[Optional["LLMProvider"]]) -> None:
    """Undo a prior set_context_provider call. The Token is typed to this
    ContextVar's value type, so a Token from a different ContextVar is a
    type error, not a silent cross-context reset. Always call this in a
    `finally` block paired with the `set_context_provider` that produced
    `token`, so a provider never outlives the request that set it."""
    _current_llm_provider.reset(token)


def get_context_provider() -> Optional["LLMProvider"]:
    """The provider set for the current context, or None if none is set -
    read by providers/llm/__init__.py:get_llm_provider() before it falls
    back to the environment-based dispatch."""
    return _current_llm_provider.get()


__all__ = ["get_context_provider", "reset_context_provider", "set_context_provider"]
