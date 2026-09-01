"""Stage 3: combine competency verdicts into one rubric-weighted score.

Uses the same weighted formula agents/scoring/agent.py already applies to
interview evaluations, so resume matching and interview scoring finally
produce commensurable numbers on one scale.

Coverage is a first-class output, not a footnote. A resume too sparse to
judge is an UNKNOWN, not a REJECT, and conflating those two is where most
false-negative risk lives - so below COVERAGE_THRESHOLD the candidate is
routed to human review rather than given a misleadingly confident low rank.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel

from schemas.rubric import AnchoredCompetency
from services.competency_verifier import CompetencyVerdict, EvidenceSufficiency

COVERAGE_THRESHOLD = 0.5
MAX_ANCHOR_SCORE = 5
STRONG_BAND_MIN = 0.75
POTENTIAL_BAND_MIN = 0.45


class AggregateResult(BaseModel):
    """The candidate's standing on one rubric version."""

    final_score: Optional[float] = None  # None when coverage is too low to rank
    coverage: float
    band: Optional[str] = None  # strong | potential | weak
    needs_human_review: bool
    scored_competencies: List[str]
    uncited_competencies: List[str]


def _band(normalized: float) -> str:
    if normalized >= STRONG_BAND_MIN:
        return "strong"
    if normalized >= POTENTIAL_BAND_MIN:
        return "potential"
    return "weak"


def aggregate(
    competencies: List[AnchoredCompetency], verdicts: List[CompetencyVerdict]
) -> AggregateResult:
    """Weighted-average the cited verdicts; report coverage separately."""
    by_name = {v.competency_name: v for v in verdicts}

    weighted_sum = 0.0
    counted_weight = 0.0
    scored: List[str] = []
    uncited: List[str] = []

    for comp in competencies:
        verdict = by_name.get(comp.name)
        # A missing verdict and an uncited one are the same thing: no
        # evidence was established, so the competency is unscored rather
        # than scored badly.
        if verdict is None or verdict.evidence_sufficiency is EvidenceSufficiency.INSUFFICIENT:
            uncited.append(comp.name)
            continue
        weighted_sum += comp.weight * verdict.score
        counted_weight += comp.weight
        scored.append(comp.name)

    total_weight = sum(c.weight for c in competencies) or 1.0
    coverage = counted_weight / total_weight

    if coverage < COVERAGE_THRESHOLD or counted_weight == 0.0:
        return AggregateResult(
            final_score=None,
            coverage=coverage,
            band=None,
            needs_human_review=True,
            scored_competencies=scored,
            uncited_competencies=uncited,
        )

    normalized = (weighted_sum / counted_weight) / MAX_ANCHOR_SCORE
    return AggregateResult(
        final_score=normalized,
        coverage=coverage,
        band=_band(normalized),
        needs_human_review=False,
        scored_competencies=scored,
        uncited_competencies=uncited,
    )
