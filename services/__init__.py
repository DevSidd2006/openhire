"""
Application service layer.

Sits between the HTTP transport (`api/`) and the domain (`utils/`,
`agents/`). Its job is orchestration across collaborators - run the
interview engine, then record the result through the persistence boundary,
then translate domain exceptions into the application's error vocabulary.

What belongs here: sequencing several collaborators for one use case,
persistence writes, authorisation checks that need domain data.

What does NOT belong here: interview intelligence of any kind. Competency
prioritisation, question generation, answer evaluation, evidence
construction and scoring stay inside utils/adaptive_interview.py,
agents/interviewer/agent.py and utils/evidence.py exactly as P3/P4 built
them. A service in this package may only call `InterviewSessionRunner`'s
public methods - it must never reimplement, second-guess or short-circuit
one.
"""
from services.interview_service import InterviewService

__all__ = ["InterviewService"]
