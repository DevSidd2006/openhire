"""Anchored rubric schemas.

A rubric is the single mechanism through which every dimension of fit is
scored - skills, trajectory, recency, culture. Expressing all of them as
competencies (rather than as parallel bolt-on scores) is what removes the
gaps between subsystems that a candidate could otherwise fall through.

Distinct from schemas/job.py's `Competency`, which carries a weight but no
written anchors. Anchors are what make a 1-5 score absolute rather than
relative, and absolute scores are what make a leaderboard comparable across
candidates scored at different times - a candidate scored today has to be
rankable against one scored last month, which a cosine similarity is not.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List

from pydantic import BaseModel, Field, field_validator

ANCHOR_LEVELS = (1, 2, 3, 4, 5)
MIN_COMPETENCIES = 3
MAX_COMPETENCIES = 6
WEIGHT_SUM_TOLERANCE = 0.001


class RubricStatus(str, Enum):
    """DRAFT is not usable for scoring. APPROVED is immutable - an edit mints
    a new version and supersedes the old one, so a leaderboard never mixes
    rows scored under different rubrics."""

    DRAFT = "draft"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class AnchoredCompetency(BaseModel):
    """One scored dimension, with written descriptors for each 1-5 level."""

    name: str
    definition: str
    weight: float = Field(ge=0.0, le=1.0)
    anchors: Dict[int, str]

    @field_validator("anchors")
    @classmethod
    def all_five_anchors_present_and_non_blank(cls, v: Dict[int, str]) -> Dict[int, str]:
        missing = [lvl for lvl in ANCHOR_LEVELS if lvl not in v]
        if missing:
            raise ValueError(f"anchors missing for level(s) {missing}; all of 1-5 required")
        blank = [lvl for lvl in ANCHOR_LEVELS if not v[lvl].strip()]
        if blank:
            raise ValueError(f"anchor text is blank for level(s) {blank}")
        return v


class JobRubric(BaseModel):
    """A versioned set of competencies for one job."""

    rubric_id: str
    job_id: str
    version: int = Field(ge=1)
    status: RubricStatus = RubricStatus.DRAFT
    competencies: List[AnchoredCompetency] = Field(default_factory=list)

    def validate_approvable(self) -> List[str]:
        """Return every reason this rubric may not be approved.

        Returns a list rather than raising so the recruiter sees all problems
        at once instead of fixing them one round-trip at a time. A malformed
        rubric silently corrupts every ranking on the job, so this gate
        rejects rather than warns.
        """
        violations: List[str] = []

        n = len(self.competencies)
        if n < MIN_COMPETENCIES:
            violations.append(
                f"rubric has {n} competencies; at least {MIN_COMPETENCIES} required"
            )
        if n > MAX_COMPETENCIES:
            violations.append(
                f"rubric has {n} competencies; at most {MAX_COMPETENCIES} allowed "
                "(scoring quality degrades beyond six)"
            )

        total = sum(c.weight for c in self.competencies)
        if self.competencies and abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            violations.append(f"competency weights sum to {total:.4f}; must sum to 1.0")

        names = [c.name.strip().lower() for c in self.competencies]
        dupes = {name for name in names if names.count(name) > 1}
        if dupes:
            violations.append(f"duplicate competency names: {sorted(dupes)}")

        return violations
