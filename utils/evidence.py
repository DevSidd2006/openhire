"""
Utility functions for evidence tracking (P2: canonical evidence model).
"""
import re
from typing import Any, Dict, List, Optional, Sequence, TYPE_CHECKING
from schemas.evaluation import EvidenceItem, EvidenceType

if TYPE_CHECKING:
    from schemas.interview import InterviewTranscript


def _slug(value: Optional[str]) -> str:
    """Turn arbitrary text into a short, filesystem/ID-safe token. Used only
    for building deterministic evidence IDs - never shown to a candidate or
    used as anything but an identifier fragment."""
    if not value:
        return "na"
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", str(value)).strip("_").lower()
    return slug or "na"


def build_evidence_id(
    candidate_id: Optional[str],
    source_key: Optional[str],
    competency: Optional[str],
    agent: str,
) -> str:
    """Build a deterministic evidence ID from the pieces that make an
    evidence item unique in practice: which candidate it's about, what it's
    sourced from (a question_id, a resume claim_id, a bias-rationale section
    key...), which competency/criterion it relates to (if any), and which
    agent produced it.

    Deterministic on purpose (P2 Phase 3): the same underlying evidence
    always gets the same ID across runs, which makes regression tests and
    reproducibility checks straightforward - unlike a random uuid, a test can
    assert on the exact ID a given (candidate, question, competency) triple
    produces. Including candidate_id also means IDs can never collide across
    candidates, which doubles as a structural guard against accidentally
    reusing one candidate's evidence for another.
    """
    parts = [_slug(candidate_id), _slug(source_key), _slug(competency), _slug(agent)]
    return "ev_" + "_".join(parts)


def create_evidence(
    source_type: str,
    text: str,
    agent: str,
    explanation: str,
    candidate_id: Optional[str] = None,
    question_id: Optional[str] = None,
    answer_id: Optional[str] = None,
    evidence_type: EvidenceType = "supporting",
    competency: Optional[str] = None,
    timestamp_start: Optional[float] = None,
    timestamp_end: Optional[float] = None,
    source_id: Optional[str] = None,
    relevance: float = 0.8,
) -> EvidenceItem:
    """Create an evidence item with a deterministic ID."""
    evidence_id = build_evidence_id(
        candidate_id=candidate_id,
        source_key=question_id or source_id,
        competency=competency,
        agent=agent,
    )
    return EvidenceItem(
        evidence_id=evidence_id,
        candidate_id=candidate_id,
        source_type=source_type,
        source_id=source_id,
        question_id=question_id,
        answer_id=answer_id,
        evidence_type=evidence_type,
        competency=competency,
        timestamp_start=timestamp_start,
        timestamp_end=timestamp_end,
        text=text,
        relevance=relevance,
        agent=agent,
        explanation=explanation,
    )


def create_transcript_evidence(
    text: str,
    question_id: str,
    agent: str,
    explanation: str,
    candidate_id: Optional[str] = None,
    answer_id: Optional[str] = None,
    evidence_type: EvidenceType = "supporting",
    competency: Optional[str] = None,
    timestamp_start: Optional[float] = None,
    timestamp_end: Optional[float] = None,
    relevance: float = 0.85,
) -> EvidenceItem:
    """Create evidence item from transcript."""
    return create_evidence(
        source_type="transcript",
        text=text,
        agent=agent,
        explanation=explanation,
        candidate_id=candidate_id,
        question_id=question_id,
        answer_id=answer_id,
        evidence_type=evidence_type,
        competency=competency,
        timestamp_start=timestamp_start,
        timestamp_end=timestamp_end,
        relevance=relevance,
    )


def create_resume_evidence(
    text: str,
    source_id: str,
    agent: str,
    explanation: str,
    candidate_id: Optional[str] = None,
    evidence_type: EvidenceType = "supporting",
    relevance: float = 0.80,
) -> EvidenceItem:
    """Create evidence item from resume."""
    return create_evidence(
        source_type="resume",
        text=text,
        source_id=source_id,
        agent=agent,
        explanation=explanation,
        candidate_id=candidate_id,
        evidence_type=evidence_type,
        relevance=relevance,
    )


def resolve_transcript_evidence(
    transcript: "InterviewTranscript",
    question_number: Optional[int],
    agent: str,
    explanation: str,
    candidate_id: Optional[str] = None,
    evidence_type: EvidenceType = "supporting",
    competency: Optional[str] = None,
    relevance: float = 0.85,
) -> Optional[EvidenceItem]:
    """Resolve a 1-indexed question number (matching the "Q1", "Q2", ... labels
    every agent's `_format_transcript` uses when building its prompt) to the
    real question/answer pair in the sealed transcript, and build an
    EvidenceItem whose `text` is the candidate's ACTUAL answer text.

    This is the standard way evaluator agents turn an LLM's claim of "this
    answer supports my judgment" into traceable evidence: the LLM only needs
    to report which numbered exchange it drew on, never the evidence text
    itself, so the evidence can never contain a fabricated quote.

    `candidate_id` defaults to `transcript.candidate_id` (the transcript
    always knows whose interview it is) if not given explicitly.

    Returns None if question_number is missing or doesn't correspond to a
    real exchange - callers must not invent a question_id in that case, and
    should treat the judgment as ungrounded (e.g. mark it
    CompetencyScore.evidence_status="insufficient", skip it, or route to
    human review) rather than fabricate evidence.
    """
    if question_number is None:
        return None
    try:
        idx = int(question_number) - 1
    except (TypeError, ValueError):
        return None
    if idx < 0 or idx >= len(transcript.exchanges):
        return None

    question, answer = transcript.exchanges[idx]
    return create_transcript_evidence(
        text=answer.answer_text,
        question_id=question.question_id,
        agent=agent,
        explanation=explanation,
        candidate_id=candidate_id if candidate_id is not None else transcript.candidate_id,
        answer_id=question.question_id,
        evidence_type=evidence_type,
        competency=competency,
        timestamp_start=answer.timestamp_start,
        timestamp_end=answer.timestamp_end,
        relevance=relevance,
    )


def validate_evidence_belongs_to_candidate(evidence: EvidenceItem, candidate_id: str) -> bool:
    """An evidence item with no candidate_id stamped is not (yet) a
    violation - callers that need a hard guarantee should also check
    `evidence.candidate_id is not None`. An evidence item stamped with a
    DIFFERENT candidate's ID is always a violation."""
    return evidence.candidate_id is None or evidence.candidate_id == candidate_id


def validate_evidence_references_real_question(
    evidence: EvidenceItem, transcript: "InterviewTranscript"
) -> bool:
    """True if transcript-sourced evidence actually points at a real
    question in the given transcript. Non-transcript evidence (resume,
    derived) trivially passes - question_id doesn't apply to it."""
    if evidence.source_type != "transcript":
        return True
    if not evidence.question_id:
        return False
    return any(q.question_id == evidence.question_id for q, _ in transcript.exchanges)


def validate_evidence_ids_unique(evidence_list: List[EvidenceItem]) -> bool:
    """True if every evidence item in the list has a distinct evidence_id."""
    ids = [e.evidence_id for e in evidence_list]
    return len(ids) == len(set(ids))


def summarize_evidence(evidence_list: List[EvidenceItem]) -> str:
    """Create a summary of evidence items."""
    if not evidence_list:
        return "No evidence provided."

    summary = f"Evidence summary ({len(evidence_list)} items):\n"
    for i, item in enumerate(evidence_list, 1):
        if item.timestamp_start is not None:
            summary += f"{i}. [{item.timestamp_start:.1f}s] {item.text[:100]}...\n"
        else:
            summary += f"{i}. {item.text[:100]}...\n"
    return summary


def is_valid_evidence(
    evidence: Optional[EvidenceItem],
    candidate_id: Optional[str] = None,
    transcript: Optional["InterviewTranscript"] = None,
) -> bool:
    """Validate that an individual evidence item is grounded and traceable.

    Reuses existing repository primitives:
    - evidence_type cannot be 'insufficient' (supporting/contradicting are valid)
    - text must be non-empty and non-whitespace
    - candidate_id (if stamped) must match candidate_id (validate_evidence_belongs_to_candidate)
    - for transcript evidence: question_id must be present and non-empty, and if
      transcript is provided, must reference a real question in the transcript
      (validate_evidence_references_real_question)
    """
    if evidence is None:
        return False
    if getattr(evidence, "evidence_type", None) == "insufficient":
        return False
    text = getattr(evidence, "text", None)
    if not text or not str(text).strip():
        return False
    if candidate_id is not None and not validate_evidence_belongs_to_candidate(evidence, candidate_id):
        return False
    if getattr(evidence, "source_type", None) == "transcript":
        question_id = getattr(evidence, "question_id", None)
        if not question_id or not str(question_id).strip():
            return False
        if transcript is not None and not validate_evidence_references_real_question(evidence, transcript):
            return False
    return True


def is_competency_evidence_backed(
    competency_score: Any,
    candidate_id: Optional[str] = None,
    transcript: Optional["InterviewTranscript"] = None,
) -> bool:
    """Check if an evaluated competency score is backed by valid, traceable evidence.

    Returns False if:
    - competency_score is None
    - evidence_status is 'insufficient'
    - evidence list is empty
    - none of the evidence items satisfy is_valid_evidence
    """
    if competency_score is None:
        return False
    if getattr(competency_score, "evidence_status", "supported") == "insufficient":
        return False
    evidence_list = getattr(competency_score, "evidence", None)
    if not evidence_list:
        return False
    return any(
        is_valid_evidence(e, candidate_id=candidate_id, transcript=transcript)
        for e in evidence_list
    )


def compute_evidence_backed_coverage(
    competency_scores: Optional[Sequence[Any]],
    rubric: Optional[Any],
    candidate_id: Optional[str] = None,
    transcript: Optional["InterviewTranscript"] = None,
) -> float:
    """Deterministic post-evaluation metric measuring evidence-backed coverage
    of evaluated competencies.

    Semantic:
        "How much of the candidate's evaluated competency profile is backed
        by valid, traceable evidence?"

    Formula:
        evidence_backed_coverage = (
            sum(weight_i for applicable evaluated i with valid evidence)
            / sum(weight_i for all applicable evaluated i)
        )

    The denominator is the total rubric weight of the candidate's applicable
    evaluated competencies (NOT the job's total competency weight). This preserves
    the strict distinction from rubric_coverage:
        - rubric_coverage: evaluates completeness against the entire job rubric
          (denominator = sum of all job competency weights)
        - evidence_backed_coverage: evaluates evidence grounding across the candidate's
          actual evaluated competency profile (denominator = sum of evaluated competency weights)

    Handles:
    - no competency scores -> 0.0
    - empty evidence -> 0.0 (or partial if other competencies have valid evidence)
    - insufficient evidence -> excluded from numerator
    - multiple evidence items -> competency counted once, not multiplied
    - duplicate evidence -> handled cleanly, counted once
    - invalid evidence -> excluded from numerator
    - missing competency matches / unmatched competencies -> unmatched excluded from rubric weights
    - zero total applicable weight -> 0.0
    - non-normalized weights -> normalized cleanly by total evaluated weight to [0.0, 1.0]

    Returns:
        float in [0.0, 1.0]
    """
    if not competency_scores or rubric is None:
        return 0.0

    rubric_weights: Dict[str, float] = {}
    if hasattr(rubric, "competencies"):
        competencies = getattr(rubric, "competencies", None)
        if competencies:
            for c in competencies:
                name = getattr(c, "name", None)
                weight = getattr(c, "weight", 0.0)
                if name:
                    rubric_weights[str(name).strip().lower()] = float(weight)
    elif isinstance(rubric, dict):
        for k, v in rubric.items():
            rubric_weights[str(k).strip().lower()] = float(v)
    elif isinstance(rubric, (list, tuple)):
        for item in rubric:
            if hasattr(item, "name") and hasattr(item, "weight"):
                rubric_weights[str(item.name).strip().lower()] = float(item.weight)

    if not rubric_weights:
        return 0.0

    # Index evaluated competency scores by normalized name (first occurrence wins)
    by_name: Dict[str, Any] = {}
    for cs in competency_scores:
        name = getattr(cs, "competency_name", None)
        if name:
            by_name.setdefault(str(name).strip().lower(), cs)

    total_evaluated_weight = 0.0
    valid_weight = 0.0

    # Iterate over rubric competencies for deterministic evaluation
    for comp_name, weight in rubric_weights.items():
        if weight <= 0:
            continue
        cs = by_name.get(comp_name)
        if cs is None:
            # Competency was not evaluated - not part of the candidate's evaluated profile
            continue
        total_evaluated_weight += weight
        if is_competency_evidence_backed(cs, candidate_id=candidate_id, transcript=transcript):
            valid_weight += weight

    if total_evaluated_weight <= 0:
        return 0.0

    coverage = valid_weight / total_evaluated_weight
    return max(0.0, min(1.0, float(coverage)))
