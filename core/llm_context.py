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
    """Set the provider for the current context (request).

    Returns a token that must be passed to reset_context_provider()
    when the request ends.
    """
    return _current_llm_provider.set(provider)


def reset_context_provider(token: Token) -> None:
    """Undo a prior set_context_provider call.

    Always call this in a finally block paired with the
    set_context_provider() call that produced the token.
    """
    _current_llm_provider.reset(token)


def get_context_provider() -> Optional["LLMProvider"]:
    """Return the provider set for the current context, or None."""
    return _current_llm_provider.get()


__all__ = [
    "get_context_provider",
    "reset_context_provider",
    "set_context_provider",
]
