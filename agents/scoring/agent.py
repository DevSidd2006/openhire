"""
Scoring Agent.
Synthesizes component evaluations into a final score using ONLY the job's own
declared competency rubric - not a hardcoded technical/behavioral split, and
not blended with resume/experience job-fit.

Scoring formula (deterministic, documented, reproducible):

    weighted_final_score = rubric_score

    rubric_score = sum(weight_i * score_i for matched i) / sum(weight_i for matched i)
    rubric_coverage = sum(weight_i for matched i) / sum(all weight_i)

For every competency in JobDescription.competencies, look for a matching
CompetencyScore (case-insensitive name match) from the technical evaluation
first, then the behavioral evaluation. A rubric competency with no matching
evaluator score is excluded from the sum (and the renormalization) rather
than defaulted to some score. If nothing matches at all (rubric_coverage ==
0), rubric_score falls back to a simple average of the raw
technical/behavioral scores, and this fallback is stated explicitly in
`explanation` - never silently hidden.

A matched CompetencyScore whose evidence_status is "insufficient" (P2: an
evaluator reported a score for this competency but no transcript evidence
could be resolved to back it up - see technical_evaluator/behavioral_evaluator)
is excluded from `matched_weight`/`weighted_sum`/`rubric_coverage` the same
way an unmatched competency is - a score with no evidence must not silently
count as a normal supported result. It IS still included in the returned
competency list, so CandidateScores.competency_scores keeps showing what the
evaluator claimed (with evidence_status="insufficient" visible) for report
transparency; it just doesn't move the number.

job_fit_score (from ResumeMatcherAgent's resume/experience match) is
deliberately NOT part of this formula. It already did its job upstream -
gating who gets shortlisted for interview at all (see
orchestration/graph.py:node_match_resumes) - and is retained here purely as
a separately-reported metric for transparency. No document, spec, or prompt
in this project specifies a ratio for blending job-fit into the final
ranking score; rather than invent one (the project's prior 45/30/25 hardcoded
split, and this agent's own prior 75/25 rubric/job-fit split, were both
unjustified guesses), the final score is scoped to exactly what IS documented:
"weighted by job competencies" (docs/multi-agent-system.md). Changing
job_fit_score without changing competency scores or the job's rubric must
never move weighted_final_score - see tests/test_scoring.py.

Integrity and bias flags never enter this computation - candidate quality
score and human-review concerns are kept conceptually separate, exactly as
before this change.
"""
from typing import Any, Dict, List, Tuple
import uuid

from agents.base import BaseAgent
from schemas.scoring import CandidateScores
from schemas.evaluation import (
    TechnicalEvaluation,
    BehavioralEvaluation,
    MatchingScore,
    CompetencyScore,
)
from schemas.job import JobDescription


class ScoringAgent(BaseAgent):
    """Synthesizes all evaluations into final scores using the job's own
    competency rubric weights."""

    def __init__(self, **kwargs):
        super().__init__(name="scoring", **kwargs)

    async def execute(
        self,
        job_description: JobDescription,
        technical_evaluation: TechnicalEvaluation,
        behavioral_evaluation: BehavioralEvaluation,
        matching_score: MatchingScore,
        candidate_id: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Calculate final candidate score from the job's declared competency rubric.

        Args:
            job_description: Job with competency rubric weights
            technical_evaluation: Technical eval component (required)
            behavioral_evaluation: Behavioral eval component (required)
            matching_score: Resume match score (required)
            candidate_id: Candidate identifier

        Returns:
            CandidateScores with all component and final scores

        Raises:
            ValueError: if any required evaluation component is missing. A
                failed/missing evaluation must never be silently scored as if
                it were an average one (P0-4) - callers (see
                orchestration/graph.py:node_score_candidates) only call this
                once all three components are available, excluding the
                candidate from scoring with an explicit reason otherwise.
        """
        self.logger.info(f"Scoring candidate {candidate_id}")

        missing = [
            name
            for name, value in (
                ("technical_evaluation", technical_evaluation),
                ("behavioral_evaluation", behavioral_evaluation),
                ("matching_score", matching_score),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                f"Cannot score candidate {candidate_id}: missing {', '.join(missing)}. "
                "A missing evaluation must not be silently scored as average."
            )

        # Raw component scores, kept for transparency/backward compatibility.
        # None of them directly drive weighted_final_score except rubric_score
        # (via _compute_rubric_score) - see module docstring.
        tech_score = self._normalize_score(technical_evaluation.technical_score)
        behav_score = self._normalize_score(behavioral_evaluation.behavioral_score)
        job_fit_score = self._normalize_score(matching_score.match_score * 10)

        rubric_score, rubric_coverage, matched_competencies, coverage_note = self._compute_rubric_score(
            job_description, technical_evaluation, behavioral_evaluation, tech_score, behav_score
        )

        # weighted_final_score IS rubric_score - job_fit_score is reported
        # separately below but does not enter this computation (see module
        # docstring for why).
        weighted_final = rubric_score

        explanation = (
            f"Final score: {weighted_final:.1f}/10 = rubric_score (100% - weighted by the job's own "
            f"competency rubric; coverage: {rubric_coverage * 100:.0f}% of rubric weight matched). "
            f"{coverage_note}"
            f"job_fit_score ({job_fit_score:.1f}/10, from resume/experience match) is reported "
            f"separately and does NOT affect this score - it already gated shortlisting. "
            f"Raw component scores for reference - Technical: {tech_score:.1f}, "
            f"Behavioral: {behav_score:.1f}, Job Fit: {job_fit_score:.1f}."
        )

        scores = CandidateScores(
            score_id=f"scores_{uuid.uuid4().hex[:8]}",
            candidate_id=candidate_id,
            job_id=job_description.job_id,
            technical_score=tech_score,
            behavioral_score=behav_score,
            job_fit_score=job_fit_score,
            weighted_final_score=weighted_final,
            competency_scores=matched_competencies,
            rubric_coverage=rubric_coverage,
            percentile_rank=0.0,  # Will be calculated in leaderboard
            explanation=explanation,
            confidence=min(technical_evaluation.confidence, behavioral_evaluation.confidence),
        )

        return {"candidate_scores": scores}

    def _compute_rubric_score(
        self,
        job_description: JobDescription,
        technical_evaluation: TechnicalEvaluation,
        behavioral_evaluation: BehavioralEvaluation,
        tech_score: float,
        behav_score: float,
    ) -> Tuple[float, float, List[CompetencyScore], str]:
        """Apply the job's own competency weights to evaluator-provided
        competency scores (case-insensitive name match, technical checked
        before behavioral). Deterministic and reproducible - see module
        docstring for the formula. Returns (rubric_score, coverage,
        matched_competency_scores, human-readable fallback note)."""
        if not job_description.competencies:
            note = "Job has no declared competency rubric; rubric_score falls back to avg(technical, behavioral). "
            return (tech_score + behav_score) / 2, 0.0, [], note

        by_name: Dict[str, CompetencyScore] = {}
        for cs in technical_evaluation.competency_scores:
            by_name.setdefault(cs.competency_name.strip().lower(), cs)
        for cs in behavioral_evaluation.competency_scores:
            by_name.setdefault(cs.competency_name.strip().lower(), cs)

        matched: List[CompetencyScore] = []
        matched_weight = 0.0
        weighted_sum = 0.0
        insufficient_count = 0
        total_weight = sum(c.weight for c in job_description.competencies)

        for comp in job_description.competencies:
            cs = by_name.get(comp.name.strip().lower())
            if cs is None:
                continue
            matched.append(cs)
            if cs.evidence_status == "insufficient":
                # Kept in `matched` for report transparency, but excluded
                # from the weighted sum/coverage - an evaluator score with no
                # resolvable evidence must not silently count as a normal
                # supported result (P2 Phase 11).
                insufficient_count += 1
                continue
            matched_weight += comp.weight
            weighted_sum += comp.weight * cs.score

        coverage = matched_weight / total_weight if total_weight else 0.0

        if matched_weight <= 0:
            note = "No rubric competency matched an evidence-backed evaluator score; rubric_score falls back to avg(technical, behavioral). "
            if insufficient_count:
                note += f"({insufficient_count} competency score(s) excluded for insufficient evidence.) "
            return (tech_score + behav_score) / 2, 0.0, matched, note

        rubric_score = weighted_sum / matched_weight
        note = f"({insufficient_count} competency score(s) excluded for insufficient evidence.) " if insufficient_count else ""
        return rubric_score, coverage, matched, note

    def _normalize_score(self, score: float) -> float:
        """Normalize score to 0-10 scale."""
        # If already roughly in 0-10 range, return as-is
        if 0 <= score <= 10:
            return score
        # If in 0-1 range (probability), scale to 0-10
        if 0 <= score <= 1:
            return score * 10
        # Otherwise, cap at 10
        return min(10, max(0, score))
