"""Process-wide single-use tracking for OAuth exchange tickets.

The ticket itself is an encrypted cookie, so verification is stateless. That
is useful across workers, but it cannot make a ticket single-use: two
requests can both verify the same cookie before either response deletes it.
This store closes that race within the process that serves the requests.

The interface is deliberately small. If OpenHire is later run with multiple
workers, the same `consume()` contract can be backed by a shared cache
without changing the route.
"""
from __future__ import annotations

import hashlib
import threading
import time


class OAuthTicketStore:
    """Atomically remember which tickets have already been exchanged."""

    def __init__(self) -> None:
        self._consumed: dict[str, float] = {}
        self._lock = threading.Lock()

    def consume(self, ticket: str, *, ttl_seconds: float) -> bool:
        """Mark `ticket` consumed and return True, or False if it was used.

        The value is hashed so the store does not retain bearer material.
        Expired entries are pruned on each call; traffic keeps the map
        bounded by the ticket TTL rather than process lifetime.
        """
        key = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
        now = time.monotonic()
        with self._lock:
            self._consumed = {
                stored_key: expires_at
                for stored_key, expires_at in self._consumed.items()
                if expires_at > now
            }
            if key in self._consumed:
                return False
            self._consumed[key] = now + max(0.0, ttl_seconds)
            return True
