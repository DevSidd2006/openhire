"""Rubric lifecycle and the screening leaderboard.

A job is not scorable until a recruiter has approved a rubric for it: the
rubric decides every ranking on the job, so an unreviewed one silently
corrupts the whole leaderboard. Drafting is automated; approving is not.

Distinct from `GET /jobs/{job_id}/leaderboard` in api/routes/jobs.py, which
ranks candidates by their COMPLETED INTERVIEW evaluation. This module's
`/match-leaderboard` ranks applicants by their RESUME score, before any
interview has happened.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from agents.rubric_generator.agent import RubricGeneratorAgent
from core.dependencies import (
    get_application_repository,
    get_candidate_repository,
    get_job_repository,
    get_rubric_repository,
)
from core.errors import BadRequestError, NotFoundError
from core.security import Principal, require_authenticated
from repositories.interfaces import (
    ApplicationRepository,
    CandidateRepository,
    JobRepository,
    RubricRepository,
)
from schemas.application import ApplicationStatus
from schemas.rubric import JobRubric
from services.resume_spans import extract_spans

router = APIRouter(prefix="/jobs", tags=["rubrics"])


# --------------------------------------------------------------------------
# Wire models
# --------------------------------------------------------------------------

class CitedSpanView(BaseModel):
    """A quoted fragment of the resume that justified a score."""

    span_id: str
    span_type: str
    text: str


class CompetencyVerdictView(BaseModel):
    competency_name: str
    score: int
    evidence_sufficiency: str
    rationale: str
    cited_spans: List[CitedSpanView]


class LeaderboardRowView(BaseModel):
    rank: Optional[int] = None
    application_id: str
    candidate_id: str
    # Resolved from the stored resume so the leaderboard shows a person
    # rather than an opaque id. Falls back to the candidate_id when no
    # resume is on file - never a blank cell.
    candidate_name: str
    email: Optional[str] = None
    skills: List[str] = Field(default_factory=list)
    total_experience_years: Optional[float] = None
    education: List[str] = Field(default_factory=list)
    session_id: Optional[str] = None
    status: str
    match_score: Optional[float] = None
    # Rubric-free resume-to-JD embedding similarity in [0, 1]. Present even
    # when `match_score` is not, which is the whole point: a job with no
    # approved rubric can still be ranked by this. None means "not computed",
    # never "scored zero" - render it as an unknown, not a bottom rank.
    semantic_score: Optional[float] = None
    coverage: float
    band: Optional[str] = None
    explanation: str
    competency_verdicts: List[CompetencyVerdictView]


class MatchLeaderboardResponse(BaseModel):
    """Ranked applicants for one job, plus everything that could not be
    ranked. Unrankable rows are returned separately rather than being given a
    misleading position - a sparse resume is an unknown, not a bottom rank."""

    job_id: str
    rubric_version: Optional[int] = None
    rows: List[LeaderboardRowView]
    needs_review: List[LeaderboardRowView]
    scoring_pending: List[LeaderboardRowView]


class RubricVersionsResponse(BaseModel):
    job_id: str
    versions: List[JobRubric]


# --------------------------------------------------------------------------
# Rubric lifecycle
# --------------------------------------------------------------------------

@router.post("/{job_id}/rubric/draft", response_model=JobRubric, status_code=201)
async def draft_rubric(
    job_id: str,
    jobs: JobRepository = Depends(get_job_repository),
    rubrics: RubricRepository = Depends(get_rubric_repository),
    principal: Principal = Depends(require_authenticated),
) -> JobRubric:
    """Draft a rubric from the job description.

    Always returns a DRAFT; drafting never makes a job scorable. The next
    version number continues the job's existing series so an edit supersedes
    rather than overwrites.
    """
    job_record = await jobs.get(job_id)
    if job_record is None:
        raise NotFoundError(
            "No job found for the given job_id",
            internal_detail=f"job_id={job_id!r} not found",
        )

    result = await RubricGeneratorAgent().execute(job_description=job_record.job)
    drafted: JobRubric = result["rubric"]

    existing = await rubrics.list_versions_for_job(job_id)
    next_version = max((r.version for r in existing), default=0) + 1

    return await rubrics.save(
        drafted.model_copy(update={"job_id": job_id, "version": next_version})
    )


@router.get("/{job_id}/rubric", response_model=JobRubric)
async def get_active_rubric(
    job_id: str,
    rubrics: RubricRepository = Depends(get_rubric_repository),
    principal: Principal = Depends(require_authenticated),
) -> JobRubric:
    """The job's single approved rubric.

    404 when none is approved: "not yet scorable" is a real, distinct state
    and must not look like an empty rubric.
    """
    rubric = await rubrics.get_approved_for_job(job_id)
    if rubric is None:
        raise NotFoundError(
            "This job has no approved rubric yet, so it is not scorable.",
            internal_detail=f"no approved rubric for job_id={job_id!r}",
        )
    return rubric


@router.get("/{job_id}/rubric/versions", response_model=RubricVersionsResponse)
async def list_rubric_versions(
    job_id: str,
    rubrics: RubricRepository = Depends(get_rubric_repository),
    principal: Principal = Depends(require_authenticated),
) -> RubricVersionsResponse:
    """Every rubric version for this job, oldest first."""
    return RubricVersionsResponse(
        job_id=job_id, versions=await rubrics.list_versions_for_job(job_id)
    )


@router.post("/{job_id}/rubric/{rubric_id}/approve", response_model=JobRubric)
async def approve_rubric(
    job_id: str,
    rubric_id: str,
    rubrics: RubricRepository = Depends(get_rubric_repository),
    principal: Principal = Depends(require_authenticated),
) -> JobRubric:
    """Approve a draft, making the job scorable.

    Reports EVERY violation at once rather than the first, so a recruiter
    fixes the rubric in one pass instead of one round-trip per problem. A
    malformed rubric is rejected here, not warned about.
    """
    rubric = await rubrics.get(rubric_id)
    if rubric is None or rubric.job_id != job_id:
        raise NotFoundError(
            "No rubric found for the given job and rubric id",
            internal_detail=f"rubric_id={rubric_id!r} job_id={job_id!r}",
        )

    violations = rubric.validate_approvable()
    if violations:
        raise BadRequestError(
            "This rubric cannot be approved: " + "; ".join(violations),
            context={"violations": violations},
        )

    return await rubrics.approve(rubric_id)


# --------------------------------------------------------------------------
# Screening leaderboard
# --------------------------------------------------------------------------

@router.get("/{job_id}/match-leaderboard", response_model=MatchLeaderboardResponse)
async def get_match_leaderboard(
    job_id: str,
    jobs: JobRepository = Depends(get_job_repository),
    applications: ApplicationRepository = Depends(get_application_repository),
    candidates: CandidateRepository = Depends(get_candidate_repository),
    rubrics: RubricRepository = Depends(get_rubric_repository),
    principal: Principal = Depends(require_authenticated),
) -> MatchLeaderboardResponse:
    """Applicants ranked by resume score against the job's approved rubric.

    Every row carries the resume text behind every competency score, so a
    position can be audited down to the sentence that produced it. Spans are
    re-derived from the stored resume rather than persisted separately - span
    ids are content-hashed and deterministic (services/resume_spans.py), so
    re-derivation is exact and cannot drift from what was cited.
    """
    if await jobs.get(job_id) is None:
        raise NotFoundError(
            "No job found for the given job_id",
            internal_detail=f"job_id={job_id!r} not found",
        )

    approved = await rubrics.get_approved_for_job(job_id)
    all_applications = await applications.list_for_job(job_id)

    ranked: List[LeaderboardRowView] = []
    needs_review: List[LeaderboardRowView] = []
    pending: List[LeaderboardRowView] = []

    for application in all_applications:
        row = await _build_row(application, candidates)

        if application.status is ApplicationStatus.SCORING_PENDING:
            pending.append(row)
        elif application.matching_score is None:
            pending.append(row)
        elif application.matching_score.needs_human_review:
            needs_review.append(row)
        else:
            ranked.append(row)

    ranked.sort(key=lambda r: r.match_score if r.match_score is not None else -1.0, reverse=True)
    for position, row in enumerate(ranked, start=1):
        row.rank = position

    # Rows with no rubric score are ordered by semantic score so a job
    # awaiting rubric approval still presents a usable ordering. They are
    # deliberately NOT given a `rank`: a similarity number is not the
    # evidence-bound ranking `rows` promises, and merging the two lists would
    # blur that distinction exactly where a recruiter is deciding.
    def _by_semantic(row: LeaderboardRowView) -> float:
        return row.semantic_score if row.semantic_score is not None else -1.0

    needs_review.sort(key=_by_semantic, reverse=True)
    pending.sort(key=_by_semantic, reverse=True)

    return MatchLeaderboardResponse(
        job_id=job_id,
        rubric_version=approved.version if approved else None,
        rows=ranked,
        needs_review=needs_review,
        scoring_pending=pending,
    )


async def _build_row(application, candidates: CandidateRepository) -> LeaderboardRowView:
    """Render one application, resolving cited span ids back to their text."""
    score = application.matching_score

    span_text = {}
    candidate_name = application.candidate_id
    email = None
    skills: List[str] = []
    total_experience_years = None
    education: List[str] = []

    candidate_record = await candidates.get(application.candidate_id)
    if candidate_record is not None and candidate_record.resume is not None:
        r = candidate_record.resume
        span_text = {s.span_id: s for s in extract_spans(r)}
        candidate_name = r.candidate_name or candidate_name
        email = r.email
        skills = list(r.skills) if r.skills else []
        total_experience_years = r.total_experience_years
        education = [
            f"{e.degree} ({e.institution})" if e.institution else e.degree
            for e in r.education
        ] if r.education else []

    verdict_views: List[CompetencyVerdictView] = []
    for verdict in (score.competency_verdicts if score else []):
        cited = [
            CitedSpanView(
                span_id=span.span_id, span_type=span.span_type, text=span.text
            )
            for span in (span_text.get(sid) for sid in verdict.cited_span_ids)
            if span is not None
        ]
        verdict_views.append(
            CompetencyVerdictView(
                competency_name=verdict.competency_name,
                score=verdict.score,
                evidence_sufficiency=verdict.evidence_sufficiency.value,
                rationale=verdict.rationale,
                cited_spans=cited,
            )
        )

    return LeaderboardRowView(
        application_id=application.application_id,
        candidate_id=application.candidate_id,
        candidate_name=candidate_name,
        email=email,
        skills=skills,
        total_experience_years=total_experience_years,
        education=education,
        session_id=application.session_id,
        status=application.status.value,
        match_score=score.match_score if score else None,
        semantic_score=application.semantic_score,
        coverage=score.coverage if score else 0.0,
        band=score.band if score else None,
        explanation=score.explanation if score else "Not scored yet.",
        competency_verdicts=verdict_views,
    )
