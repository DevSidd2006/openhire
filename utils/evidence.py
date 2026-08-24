"""
Utility functions for evidence tracking.
"""
import uuid
from typing import List, Optional
from schemas.evaluation import EvidenceItem


def create_evidence(
    source_type: str,
    text: str,
    agent: str,
    explanation: str,
    question_id: Optional[str] = None,
    timestamp_start: Optional[float] = None,
    timestamp_end: Optional[float] = None,
    source_id: Optional[str] = None,
    relevance: float = 0.8,
) -> EvidenceItem:
    """Create an evidence item."""
    return EvidenceItem(
        evidence_id=f"ev_{uuid.uuid4().hex[:8]}",
        source_type=source_type,
        source_id=source_id,
        question_id=question_id,
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
        question_id=question_id,
        timestamp_start=timestamp_start,
        timestamp_end=timestamp_end,
        relevance=relevance,
    )


def create_resume_evidence(
    text: str,
    source_id: str,
    agent: str,
    explanation: str,
    relevance: float = 0.80,
) -> EvidenceItem:
    """Create evidence item from resume."""
    return create_evidence(
        source_type="resume",
        text=text,
        source_id=source_id,
        agent=agent,
        explanation=explanation,
        relevance=relevance,
    )


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
