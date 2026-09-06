"""
Operator script: promote an existing user to `user_type == 'admin'`.

Run manually against the target database - this is a deploy-time operator
action, not an application feature (design spec §4). There is no
self-service path to becoming an admin anywhere in this codebase; this
script and `PATCH /admin/users/{id}` (once at least one admin exists) are
the only two ways `user_type` ever becomes 'admin'.

Usage:
    python -m scripts.promote_admin someone@example.com

Reads DATABASE_URL (and any other AppSettings env vars) from the
environment exactly like the running service does - see
core/config.py:AppSettings.from_env. Against an empty/unset DATABASE_URL
this promotes a user in the ephemeral in-memory store, which is only useful
for local testing of this script itself; a real promotion always needs
DATABASE_URL pointed at the deployed database.
"""
from __future__ import annotations

import asyncio
import sys

from core.config import AppSettings
from core.container import build_default_container


async def promote(email: str) -> None:
    settings = AppSettings.from_env()
    container = build_default_container(settings)
    try:
        user = await container.user_repository.get_by_email(email)
        if user is None:
            print(f"No account found for email {email!r}.", file=sys.stderr)
            sys.exit(1)
        if user.user_type == "admin":
            print(f"{email} is already an admin. Nothing to do.")
            return
        updated = user.model_copy(update={"user_type": "admin"})
        await container.user_repository.save(updated)
        print(f"Promoted {email} (user_id={user.user_id}) to admin.")
    finally:
        await container.aclose()


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.promote_admin <email>", file=sys.stderr)
        sys.exit(2)
    asyncio.run(promote(sys.argv[1]))


if __name__ == "__main__":
    main()
