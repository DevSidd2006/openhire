"""
Candidate endpoints.

Thin translators, same pattern as api/routes/jobs.py: no resume-parsing
logic lives here - that stays inside `agents/resume_parser/agent.py` via
`services/candidate_service.py`.
"""
from fastapi import APIRouter, Depends

from api.models_candidates import (
    CandidateListResponse,
    CandidateResponse,
    RegisterCandidateRequest,
    UpdateCandidateRequest,
)
from core.dependencies import get_candidate_service
from core.security import Principal, require_authenticated
from services.candidate_service import CandidateService

router = APIRouter(prefix="/candidates", tags=["candidates"])


@router.post("", response_model=CandidateResponse, status_code=201)
async def register_candidate(
    payload: RegisterCandidateRequest,
    service: CandidateService = Depends(get_candidate_service),
    principal: Principal = Depends(require_authenticated),
) -> CandidateResponse:
    """POST /candidates - parse raw resume text (via the existing
    `ResumeParserAgent`) into a structured candidate profile and store it.

    Errors: 503 `dependency_unavailable` only if even the parser's own
    deterministic fallback fails (a genuine unexpected error - see
    agents/resume_parser/agent.py's module docstring); 422 for a missing
    `resume_text`/`candidate_name`.
    """
    record = await service.register_candidate(
        resume_text=payload.resume_text,
        candidate_name=payload.candidate_name,
        candidate_id=payload.candidate_id,
    )
    return CandidateResponse.from_record(record)


@router.get("/{candidate_id}", response_model=CandidateResponse)
async def get_candidate(
    candidate_id: str,
    service: CandidateService = Depends(get_candidate_service),
    principal: Principal = Depends(require_authenticated),
) -> CandidateResponse:
    """GET /candidates/{candidate_id}. Errors: 404 `not_found`."""
    record = await service.get_candidate(candidate_id)
    return CandidateResponse.from_record(record)


@router.get("", response_model=CandidateListResponse)
async def list_candidates(
    service: CandidateService = Depends(get_candidate_service),
    principal: Principal = Depends(require_authenticated),
) -> CandidateListResponse:
    """GET /candidates - every registered candidate.

    Recruiter-facing in intent (a candidate has no reason to browse other
    candidates' profiles), but not yet scoped that way: real role
    enforcement needs the identity persistence the auth/database teammate
    owns (see core/security.py and the Chunk 2 handoff). Flagged as a known
    gap, not silently assumed safe.
    """
    records = await service.list_candidates()
    return CandidateListResponse.from_records(records)


@router.patch("/{candidate_id}", response_model=CandidateResponse)
async def update_candidate(
    candidate_id: str,
    payload: UpdateCandidateRequest,
    service: CandidateService = Depends(get_candidate_service),
    principal: Principal = Depends(require_authenticated),
) -> CandidateResponse:
    """PATCH /candidates/{candidate_id} - re-parse (`resume_text`) and/or a
    direct field edit; see services/candidate_service.py:update_candidate
    for exactly how the two combine.

    Errors: 404 `not_found`; 503 `dependency_unavailable` if a supplied
    `resume_text` fails to parse even via the fallback.
    """
    data = payload.model_dump(exclude_unset=True)
    resume_text = data.pop("resume_text", None)
    record = await service.update_candidate(candidate_id, resume_text=resume_text, patch=data)
    return CandidateResponse.from_record(record)
