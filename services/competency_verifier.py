"""Stage 2: score each competency against its anchors, citing resume spans.

The load-bearing rule of the whole matcher lives in `validate_citations`: a
score whose cited span IDs do not exist in what was actually retrieved is
coerced to INSUFFICIENT and excluded from the weighted score - not counted
as zero, not silently averaged in.

That single rule does most of the work of closing rubric loopholes:
hallucinated evidence cannot survive validation; absent evidence is reported
as absent rather than disguised as a low score; and keyword stuffing fails,
because a bare skills line cannot support a Proficient-or-above anchor that
requires demonstrated application.
"""
from __future__ import annotations

from typing import Set


from schemas.rubric import CompetencyVerdict, EvidenceSufficiency

__all__ = ["CompetencyVerdict", "EvidenceSufficiency", "validate_citations"]


def validate_citations(
    verdict: CompetencyVerdict, retrieved_span_ids: Set[str]
) -> CompetencyVerdict:
    """Drop citations that do not correspond to a retrieved span.

    If nothing survives, the verdict becomes INSUFFICIENT regardless of the
    score the model assigned, and aggregation will exclude it from the
    weighted average rather than treating it as a low score.
    """
    real = [sid for sid in verdict.cited_span_ids if sid in retrieved_span_ids]
    dropped = len(verdict.cited_span_ids) - len(real)

    if not real:
        return verdict.model_copy(
            update={
                "cited_span_ids": [],
                "evidence_sufficiency": EvidenceSufficiency.INSUFFICIENT,
                "rationale": (
                    f"{verdict.rationale} [Coerced to insufficient: score was uncited or "
                    f"cited {dropped} span(s) not present in the retrieved evidence.]"
                ),
            }
        )

    if dropped:
        return verdict.model_copy(
            update={
                "cited_span_ids": real,
                "rationale": (
                    f"{verdict.rationale} [Dropped {dropped} invalid citation(s) during "
                    "citation validation.]"
                ),
            }
        )

    return verdict
