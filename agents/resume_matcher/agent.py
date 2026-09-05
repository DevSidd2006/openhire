"""Evidence-bound resume matcher.

Replaces the previous fixed-weight similarity scorer (50% skills / 20% JD
similarity / 30% experience). That design could not distinguish a claim from
evidence - a resume mentioning Kubernetes scored the same whether the
candidate ran production clusters or attended a webinar - which made keyword
stuffing a winning strategy and left no ranking auditable.

Here every competency in the job's rubric is scored 1-5 against written
anchors, and every score must cite the resume spans that justify it. Scores
citing nothing real are excluded rather than counted as zero.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List

from pydantic import BaseModel

from agents.base import BaseAgent
from schemas.evaluation import MatchingScore
from schemas.resume import ParsedResume
from schemas.rubric import CompetencyVerdict, JobRubric
from services.competency_verifier import validate_citations
from services.resume_spans import ResumeSpan, extract_spans
from services.rubric_aggregation import aggregate
from services.semantic_matching import SemanticMatcher

RETRIEVAL_K = 8


class _VerdictBatch(BaseModel):
    verdicts: List[CompetencyVerdict]


class ResumeMatcherAgent(BaseAgent):
    """Scores a resume against a job's approved rubric, with citations."""

    def __init__(self, semantic_matcher=None, **kwargs):
        super().__init__(name="resume_matcher", **kwargs)
        self.semantic_matcher = semantic_matcher or SemanticMatcher()

    async def execute(
        self, job_rubric: JobRubric, parsed_resume: ParsedResume, **kwargs
    ) -> Dict[str, Any]:
        """Score one candidate against one rubric version.

        Deliberately does NOT catch exceptions: an embedding outage or LLM
        failure must propagate so the caller can mark the application
        SCORING_PENDING. Swallowing them here would turn an infrastructure
        failure into a low score, and a low score into a rejection.
        """
        self.logger.info(
            f"Matching candidate {parsed_resume.candidate_id} against rubric "
            f"{job_rubric.rubric_id} v{job_rubric.version}"
        )

        spans = extract_spans(parsed_resume)

        retrieved: Dict[str, List[ResumeSpan]] = {}
        for comp in job_rubric.competencies:
            retrieved[comp.name] = await self.semantic_matcher.retrieve_spans_for_competency(
                comp, spans, k=RETRIEVAL_K
            )

        verdicts = await self._verify(job_rubric, retrieved, parsed_resume)
        result = aggregate(job_rubric.competencies, verdicts)

        return {
            "matching_score": MatchingScore(
                match_id=f"match_{uuid.uuid4().hex[:8]}",
                candidate_id=parsed_resume.candidate_id,
                job_id=job_rubric.job_id,
                rubric_version=job_rubric.version,
                match_score=result.final_score,
                coverage=result.coverage,
                band=result.band,
                competency_verdicts=verdicts,
                needs_human_review=result.needs_human_review,
                explanation=self._explain(result, verdicts),
                confidence=result.coverage,
            ),
            "needs_human_review": result.needs_human_review,
        }

    async def _verify(
        self, rubric: JobRubric, retrieved: Dict[str, List[ResumeSpan]], resume: ParsedResume
    ) -> List[CompetencyVerdict]:
        """One LLM call scoring every competency, then citation validation."""
        prompt = self._build_prompt(rubric, retrieved, resume)

        batch = await self.call_llm_structured(
            prompt, _VerdictBatch.model_json_schema(), validate=_VerdictBatch.model_validate
        )

        validated: List[CompetencyVerdict] = []
        for verdict in batch.verdicts:
            allowed = {s.span_id for s in retrieved.get(verdict.competency_name, [])}
            validated.append(validate_citations(verdict, allowed))
        return validated

    def _build_prompt(
        self, rubric: JobRubric, retrieved: Dict[str, List[ResumeSpan]], resume: ParsedResume
    ) -> str:
        template = self.load_prompt("competency_verifier.md")
        blocks = []
        for comp in rubric.competencies:
            spans_block = "\n".join(
                f"[{s.span_id}] ({s.span_type}) {s.text}" for s in retrieved.get(comp.name, [])
            ) or "(no evidence retrieved for this competency)"
            blocks.append(
                template.format(
                    competency_name=comp.name,
                    competency_definition=comp.definition,
                    anchor_1=comp.anchors[1], anchor_2=comp.anchors[2],
                    anchor_3=comp.anchors[3], anchor_4=comp.anchors[4],
                    anchor_5=comp.anchors[5],
                    spans_block=spans_block,
                    structured_features=(
                        f"Total experience: {resume.total_experience_years or 'unknown'} years; "
                        f"{len(resume.work_experience)} roles listed"
                    ),
                )
            )
        return "\n\n---\n\n".join(blocks) + '\n\nReturn JSON: {"verdicts": [...]}'

    @staticmethod
    def _explain(result, verdicts: List[CompetencyVerdict]) -> str:
        if result.needs_human_review:
            return (
                f"Insufficient resume evidence to rank this candidate "
                f"(coverage {result.coverage:.0%}). Unscored: "
                f"{', '.join(result.uncited_competencies)}. Routed to human review "
                "rather than scored low - a sparse resume is an unknown, not a reject."
            )
        parts = [
            f"{v.competency_name}: {v.score}/5 ({len(v.cited_span_ids)} cited span(s))"
            for v in verdicts
            if v.competency_name in result.scored_competencies
        ]
        return f"Band {result.band}, coverage {result.coverage:.0%}. " + "; ".join(parts)
