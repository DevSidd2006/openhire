"""Semantic screening: a rubric-free resume-to-JD similarity score.

Complements, and never replaces, the evidence-bound rubric matcher in
`services/matching_service.py`. That matcher is the authority on whether a
candidate meets a competency, and every one of its scores is backed by a
cited resume span. This score is a much weaker signal - plain embedding
cosine similarity between the whole resume and the whole job description -
and it exists for one reason: a job with no approved rubric is deliberately
not scorable, so before a recruiter approves one there is otherwise nothing
at all to rank applicants by.

The two are stored side by side (`Application.semantic_score` vs
`Application.matching_score`) rather than merged, because merging would let
an unaudited similarity number silently move a rank that the evidence-bound
contract promises is explainable down to the sentence.

Failure policy matches `services/semantic_matching.py`: an embedding outage
yields None, never 0.0. A 0.0 is indistinguishable from a genuinely poor
candidate, so swallowing an outage here would turn infrastructure trouble
into a wave of bad ranks.
"""
from __future__ import annotations

from typing import Optional

from core.logging import get_logger, log_context
from schemas.job import JobDescription
from schemas.resume import ParsedResume
from services.resume_spans import extract_spans
from services.semantic_matching import EmbeddingUnavailableError, SemanticMatcher

logger = get_logger("services.semantic_screening")


def render_resume_text(resume: ParsedResume) -> str:
    """Flatten a parsed resume into one embeddable string.

    Reuses `extract_spans` - the same fragments the evidence-bound matcher
    cites - so this score is computed over exactly the text a recruiter can
    later be shown, with no separately-maintained second view of the resume.
    """
    return "\n".join(span.text for span in extract_spans(resume))


def render_job_text(job: JobDescription) -> str:
    """Flatten a job description into one embeddable string."""
    parts = [job.title, job.description]
    if job.required_skills:
        parts.append("Required skills: " + ", ".join(job.required_skills))
    if job.preferred_skills:
        parts.append("Preferred skills: " + ", ".join(job.preferred_skills))
    if job.required_qualifications:
        parts.append("Required qualifications: " + "; ".join(job.required_qualifications))
    if job.responsibilities:
        parts.append("Responsibilities: " + "; ".join(job.responsibilities))
    return "\n".join(p for p in parts if p)


class SemanticScreeningService:
    """Computes `Application.semantic_score` for one candidate and job."""

    def __init__(self, *, semantic_matcher: Optional[SemanticMatcher] = None) -> None:
        # Injectable so the outage path is testable without a live provider,
        # and resolved lazily so merely constructing an ApplicationService
        # does not build an embedding client for a job that never scores.
        self._matcher = semantic_matcher

    def _get_matcher(self) -> SemanticMatcher:
        if self._matcher is None:
            self._matcher = SemanticMatcher()
        return self._matcher

    async def score(
        self, job: JobDescription, resume: ParsedResume
    ) -> Optional[float]:
        """Similarity of `resume` to `job` in [0, 1], or None if unavailable.

        None means "not computed" - an embedding outage, or a resume or job
        with no usable text. It never means "scored zero"; callers must
        render it as an unknown rather than as a bottom rank.
        """
        resume_text = render_resume_text(resume)
        job_text = render_job_text(job)
        if not resume_text or not job_text:
            logger.info(
                "semantic screening skipped: no text to compare",
                extra=log_context(
                    event="semantic_screening_skipped",
                    job_id=job.job_id,
                    candidate_id=resume.candidate_id,
                ),
            )
            return None

        try:
            similarity = await self._get_matcher().calculate_job_description_similarity(
                resume_text, job_text
            )
            # Cosine similarity is defined on [-1, 1], not [0, 1] as the
            # helper's docstring claims - opposed embeddings genuinely return
            # a negative. Clamp rather than pass it on: this value is rendered
            # as a percentage, and "-13% match" is not a thing. A negative and
            # a zero both mean "no detectable similarity", which is a real
            # (bad) score and stays distinct from None, which means the score
            # could not be computed at all.
            return max(0.0, min(1.0, float(similarity)))
        except Exception as exc:  # noqa: BLE001 - see below; this must never propagate
            # Deliberately swallowed to None: this score is supplementary, so
            # an outage here must degrade the leaderboard's extra column, not
            # fail the whole matching run that the rubric score depends on.
            #
            # Broader than EmbeddingUnavailableError on purpose. Resolving the
            # provider is itself fallible and raises plain ValueError when the
            # deployment has no embedding credentials configured
            # (providers/embeddings/__init__.py), which is the normal state of
            # a deployment that has never used embeddings. Catching only the
            # narrow error there would turn a missing optional API key into a
            # 500 on POST /jobs/{job_id}/match - breaking rubric scoring, which
            # does not depend on embeddings at all.
            logger.error(
                "semantic screening unavailable: %s",
                exc,
                extra=log_context(
                    event="semantic_screening_unavailable",
                    job_id=job.job_id,
                    candidate_id=resume.candidate_id,
                ),
            )
            return None


__all__ = ["SemanticScreeningService", "render_job_text", "render_resume_text"]
