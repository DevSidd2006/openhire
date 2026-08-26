"""
Persistence boundary.

This package is the ONLY place the backend is allowed to talk about storage.
It contains two things and nothing else:

    interfaces.py   the contracts the service layer depends on
    memory.py       temporary in-process adapters implementing those
                    contracts, to be REPLACED by database-backed ones

The database schema is owned by another teammate. Nothing here declares a
table, a column, a relationship, or an ORM model, and nothing here should
be treated as a proposal for one. The interfaces are derived strictly from
what the *existing* backend already does today - it keeps live
`InterviewSessionRunner` objects in a dict, and it produces a sealed
`InterviewTranscript` that currently has nowhere to go - not from a guess
about the eventual data model.

Replacing the stubs later is a two-line change in `core/container.py`; no
service, route, or domain module needs to change, because none of them
import from `repositories.memory`.
"""
from repositories.interfaces import (
    RepositoryError,
    SessionRecord,
    SessionRepository,
    TranscriptRepository,
)

__all__ = [
    "RepositoryError",
    "SessionRecord",
    "SessionRepository",
    "TranscriptRepository",
]
