"""Shared rubric fixtures for pipeline tests.

Matching is rubric-driven and a job with no APPROVED rubric is deliberately
not scorable, so any test that drives the pipeline through matching has to
supply one.
"""
from schemas.rubric import AnchoredCompetency, JobRubric, RubricStatus

_ANCHORS = {
    1: "no evidence on the resume",
    2: "mentioned but not demonstrated",
    3: "applied it in real work",
    4: "led work using it",
    5: "recognized depth of expertise",
}


def approved_rubric(job_id: str = "job_001", version: int = 1) -> JobRubric:
    """A minimal valid, approved rubric usable by any pipeline test."""
    return JobRubric(
        rubric_id=f"rub_{job_id}_v{version}",
        job_id=job_id,
        version=version,
        status=RubricStatus.APPROVED,
        competencies=[
            AnchoredCompetency(
                name="Python", definition="Writes production Python",
                weight=0.4, anchors=_ANCHORS,
            ),
            AnchoredCompetency(
                name="System design", definition="Designs maintainable systems",
                weight=0.35, anchors=_ANCHORS,
            ),
            AnchoredCompetency(
                name="Ownership", definition="Drives work to completion",
                weight=0.25, anchors=_ANCHORS,
            ),
        ],
    )
