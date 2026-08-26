"""
Evaluation API response models.

`EvaluationJob` (repositories/interfaces.py) is nested directly, including
its `result: Optional[CandidateReport]` - `CandidateReport` (schemas/scoring.py)
is already the existing, complete final evaluation artifact and has no
field that needs hiding from the same audience `GET /sessions/{id}` and
`GET /applications/{id}` are already exposed to (see that module's
docstring for the precedent: reuse the domain schema directly, no narrowing
view, when nothing in it needs redacting for THIS endpoint's caller).

Role-based redaction (e.g. should a candidate see their own
`integrity_flags`/`bias_flags`?) is a real, deliberately deferred product
decision - not something this chunk can enforce, since it needs the
identity/role system Chunk 1 already flagged as blocked on the auth/database
teammate. `require_authenticated` gates these routes exactly as every other
route in this backend is gated, and no more.
"""
from typing import Optional

from pydantic import BaseModel

from repositories.interfaces import EvaluationJob


class EvaluationJobResponse(BaseModel):
    evaluation: EvaluationJob

    @classmethod
    def from_domain(cls, job: EvaluationJob) -> "EvaluationJobResponse":
        return cls(evaluation=job)


class SessionEvaluationResponse(BaseModel):
    """`GET /sessions/{session_id}/evaluation`'s body.

    `evaluation=None` means "not triggered yet" - a normal, valid state
    (the interview may still be in progress, or its transcript may not
    have finished persisting) - distinct from the session itself not
    existing, which is still a 404 exactly as every other `/sessions/{id}`
    endpoint already behaves.
    """

    session_id: str
    evaluation: Optional[EvaluationJob] = None


__all__ = ["EvaluationJobResponse", "SessionEvaluationResponse"]
