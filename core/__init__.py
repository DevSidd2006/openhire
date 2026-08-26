"""
Backend application framework layer (Chunk 1).

This package holds the *plumbing* of the backend service - configuration,
logging, lifecycle, dependency injection, middleware, error translation and
the authentication boundary. It deliberately contains NO interview
intelligence, NO agent logic, NO prompt text and NO scoring.

The domain layers it serves are unchanged and remain the source of truth:

    utils/interview_session.py   session lifecycle + sequencing (P4)
    utils/adaptive_interview.py  adaptive decision logic (P3)
    agents/**                    LLM-calling agents
    providers/**                 LLM / audio / embedding / vector providers
    utils/evidence.py            canonical evidence construction

Nothing in `core/` may import from those modules for anything other than
type annotations and wiring.
"""
