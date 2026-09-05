"""
P6 Phase 18: reusable cross-agent evidence-grounding check.

This is the project-wide invariant the spec asks for: given ANY object (or
list of objects) that carries evidence - a TechnicalEvaluation, a
BehavioralEvaluation, a list of ClaimVerification, an IntegrityEvaluation,
a CandidateReport, or a bare list of EvidenceItem - verify that every
EvidenceItem it references:

    1. points at a question that actually exists in the transcript
    2. (equivalently) points at an answer that actually exists
    3. is stamped with the correct candidate_id
    4. belongs to an evaluation stamped with the correct job_id
    5. has text that is traceable to what the candidate actually said

Built entirely on P2's existing evidence-validation primitives
(utils/evidence.py) - this module does not reimplement grounding logic, it
composes the existing checks into one reusable "find every violation"
sweep that any evaluation adapter can call.
"""
from typing import Any, List, Optional

from schemas.evaluation import EvidenceItem
from utils.evidence import (
    validate_evidence_belongs_to_candidate,
    validate_evidence_references_real_question,
)


def _collect_evidence(target: Any) -> List[EvidenceItem]:
    """Flatten whatever shape `target` is into a plain list of
    EvidenceItem - a single evaluation object, a list of evaluation
    objects, a single EvidenceItem, or a list of EvidenceItem."""
    if target is None:
        return []
    if isinstance(target, EvidenceItem):
        return [target]
    if isinstance(target, (list, tuple)):
        collected: List[EvidenceItem] = []
        for item in target:
            collected.extend(_collect_evidence(item))
        return collected
    evidence = getattr(target, "evidence", None)
    if evidence is not None:
        return list(evidence)
    return []


def _expected_job_id(target: Any, fallback: Optional[str]) -> Optional[str]:
    if isinstance(target, (list, tuple)):
        for item in target:
            job_id = getattr(item, "job_id", None)
            if job_id:
                return job_id
        return fallback
    return getattr(target, "job_id", None) or fallback


def check_evidence_grounding(
    target: Any,
    *,
    transcript=None,
    candidate_id: Optional[str] = None,
    job_id: Optional[str] = None,
) -> List[str]:
    """Return a list of human-readable violation strings (empty = fully
    grounded). Never raises - a malformed target simply yields no evidence
    to check (an empty evidence list is not itself a grounding violation;
    callers that need to also assert evidence was actually PRODUCED should
    check that separately with a length/nonempty metric)."""
    violations: List[str] = []
    evidence_items = _collect_evidence(target)
    expected_job_id = _expected_job_id(target, job_id)

    for item in evidence_items:
        if not item.text or not item.text.strip():
            violations.append(f"{item.evidence_id}: empty evidence text")

        if candidate_id is not None and not validate_evidence_belongs_to_candidate(item, candidate_id):
            violations.append(
                f"{item.evidence_id}: candidate_id mismatch (evidence={item.candidate_id!r}, expected={candidate_id!r})"
            )

        if expected_job_id is not None and item.source_type == "transcript" and transcript is not None:
            if transcript.job_id != expected_job_id:
                violations.append(
                    f"{item.evidence_id}: transcript job_id ({transcript.job_id!r}) != expected ({expected_job_id!r})"
                )

        if item.source_type == "transcript" and transcript is not None:
            if not validate_evidence_references_real_question(item, transcript):
                violations.append(
                    f"{item.evidence_id}: question_id {item.question_id!r} does not exist in the transcript"
                )
            else:
                # Question exists -> answer exists (they are the same pair
                # in this codebase's InterviewTranscript.exchanges) and its
                # text must be traceable: the evidence text must be exactly
                # what that answer actually said, never a paraphrase or
                # invention.
                real_answer = next(
                    (a.answer_text for q, a in transcript.exchanges if q.question_id == item.question_id),
                    None,
                )
                if real_answer is not None and item.text != real_answer:
                    violations.append(
                        f"{item.evidence_id}: evidence text does not match the real answer for "
                        f"question {item.question_id!r}"
                    )

    return violations
