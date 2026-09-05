"""Chunk a parsed resume into individually citable spans.

Span IDs are the backbone of the evidence system: Stage 2 scores cite them,
Stage 3 validates those citations, and the leaderboard renders the quoted
text behind every score. They must therefore be deterministic - a span ID
that shifted between scoring runs would silently invalidate stored
citations, so IDs are derived from a content hash plus an ordinal rather
than from a UUID.
"""
from __future__ import annotations

import hashlib
from typing import List, Tuple

from pydantic import BaseModel

from schemas.resume import ParsedResume


class ResumeSpan(BaseModel):
    """One verbatim, citable fragment of a resume."""

    span_id: str
    span_type: str  # summary | skills | responsibility | achievement | project | education
    text: str


def _span_id(span_type: str, text: str, ordinal: int) -> str:
    digest = hashlib.sha1(f"{span_type}:{text}".encode("utf-8")).hexdigest()[:8]
    return f"sp_{ordinal:03d}_{digest}"


def extract_spans(resume: ParsedResume) -> List[ResumeSpan]:
    """Return every non-blank citable fragment of `resume`, in stable order."""
    raw: List[Tuple[str, str]] = []

    if resume.summary:
        raw.append(("summary", resume.summary))
    if resume.skills:
        raw.append(("skills", ", ".join(resume.skills)))

    for exp in resume.work_experience:
        header = f"{exp.position} at {exp.company} ({exp.start_year}-{exp.end_year or 'present'})"
        if exp.description:
            raw.append(("responsibility", f"{header}: {exp.description}"))
        for responsibility in exp.responsibilities:
            raw.append(("responsibility", responsibility))
        for achievement in exp.achievements:
            raw.append(("achievement", achievement))

    for proj in resume.projects:
        tech = f" [{', '.join(proj.technologies)}]" if proj.technologies else ""
        raw.append(("project", f"{proj.name}: {proj.description}{tech}"))

    for edu in resume.education:
        raw.append(("education", f"{edu.degree} in {edu.field_of_study}, {edu.institution}"))

    spans: List[ResumeSpan] = []
    for ordinal, (span_type, text) in enumerate(raw):
        stripped = text.strip()
        if not stripped:
            continue
        spans.append(
            ResumeSpan(
                span_id=_span_id(span_type, stripped, ordinal),
                span_type=span_type,
                text=stripped,
            )
        )
    return spans
