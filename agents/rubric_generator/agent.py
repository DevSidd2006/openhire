"""Stage 0: draft an anchored rubric from a job description.

The output is always a DRAFT. A rubric decides every ranking on its job, so
a bad auto-generated rubric silently corrupts the whole leaderboard - which
makes an unreviewed rubric a loophole in its own right. A recruiter must
approve it before any candidate can be scored.

All dimensions of fit - skills, career trajectory, skill recency, culture -
are drafted as competencies here rather than as separate parallel scores.
One mechanism, one evidence standard, no gaps between subsystems for a
candidate to fall through.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from agents.base import BaseAgent
from schemas.job import JobDescription
from schemas.rubric import (
    MAX_COMPETENCIES,
    MIN_COMPETENCIES,
    AnchoredCompetency,
    JobRubric,
    RubricStatus,
)


class _LLMCompetency(BaseModel):
    name: str
    definition: str
    weight: float
    anchors: Dict[str, str]  # the model returns string keys in JSON


class _LLMRubric(BaseModel):
    competencies: List[_LLMCompetency] = Field(
        min_length=MIN_COMPETENCIES, max_length=MAX_COMPETENCIES
    )


class RubricGeneratorAgent(BaseAgent):
    """Drafts an anchored, weighted rubric from job description text."""

    def __init__(self, **kwargs):
        super().__init__(name="rubric_generator", **kwargs)

    async def execute(self, job_description: JobDescription, **kwargs) -> Dict[str, Any]:
        self.logger.info(f"Drafting rubric for job {job_description.job_id}")

        prompt = self.load_prompt("rubric_generator.md").format(
            title=job_description.title,
            description=job_description.description,
            required_skills=", ".join(job_description.required_skills),
            preferred_skills=", ".join(job_description.preferred_skills),
            experience_years=job_description.experience_years or "unspecified",
            responsibilities="\n".join(f"- {r}" for r in job_description.responsibilities),
        )

        drafted = await self.call_llm_structured(
            prompt,
            _LLMRubric.model_json_schema(),
            validate=_LLMRubric.model_validate,
        )

        return {
            "rubric": JobRubric(
                rubric_id=f"rub_{uuid.uuid4().hex[:8]}",
                job_id=job_description.job_id,
                version=1,
                status=RubricStatus.DRAFT,
                competencies=self._normalize(drafted.competencies),
            )
        }

    @staticmethod
    def _normalize(raw: List[_LLMCompetency]) -> List[AnchoredCompetency]:
        """Rescale weights to sum to exactly 1.0.

        Models routinely emit weights summing to 0.95 or 1.5. Rescaling
        preserves the model's intended RELATIVE emphasis while satisfying the
        approval gate, which is better than rejecting an otherwise sound
        draft over arithmetic the recruiter did not cause.
        """
        total = sum(c.weight for c in raw) or 1.0
        return [
            AnchoredCompetency(
                name=c.name,
                definition=c.definition,
                weight=c.weight / total,
                anchors={int(level): text for level, text in c.anchors.items()},
            )
            for c in raw
        ]
