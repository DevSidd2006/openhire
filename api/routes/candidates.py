"""
Candidate endpoints.

Thin translators, same pattern as api/routes/jobs.py: no resume-parsing
logic lives here - that stays inside `agents/resume_parser/agent.py` via
`services/candidate_service.py`.
"""
from fastapi import APIRouter, Depends, File, Form, UploadFile

from api.models_candidates import (
    CandidateListResponse,
    CandidateResponse,
    ParseResumeFileResponse,
    RegisterCandidateRequest,
    UpdateCandidateRequest,
)
from core.dependencies import get_candidate_service
from core.errors import BadRequestError
from core.security import Principal, require_authenticated
from services.candidate_service import CandidateService
from utils.resume_documents import (
    ResumeExtractionError,
    UnsupportedResumeFormatError,
    extract_resume_text,
)

router = APIRouter(prefix="/candidates", tags=["candidates"])

# Resumes are text-dominant documents; 5 MB is generous headroom over any
# real resume while still well under AppSettings.max_request_body_bytes'
# global default (12 MB) - a friendlier, format-specific message instead of
# just letting the global body-size limit reject it anonymously.
_MAX_RESUME_FILE_BYTES = 5 * 1024 * 1024


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

    The candidate is linked to the authenticated user (principal.subject_id).
    When AUTH_ENABLED=false (tests/dev), subject_id is None, so we use a
    default test user_id.
    """
    user_id = principal.subject_id or "user_anonymous"
    record = await service.register_candidate(
        resume_text=payload.resume_text,
        candidate_name=payload.candidate_name,
        candidate_id=payload.candidate_id,
        user_id=user_id,
    )
    return CandidateResponse.from_record(record)


@router.post("/parse-resume-file", response_model=ParseResumeFileResponse)
async def parse_resume_file(
    resume_file: UploadFile = File(...),
    candidate_name: str = Form(..., min_length=1, max_length=200),
    service: CandidateService = Depends(get_candidate_service),
    principal: Principal = Depends(require_authenticated),
) -> ParseResumeFileResponse:
    """POST /candidates/parse-resume-file - upload a .pdf/.docx resume,
    extract its plain text, and run the EXISTING `ResumeParserAgent` on it
    for a preview the candidate can review/edit.

    Does NOT create a candidate record - that still only happens via the
    existing `POST /candidates` (unmodified), once the candidate confirms
    the (possibly edited) text this endpoint extracted. `candidate_name` is
    required here for the same reason it is on `POST /candidates`: the
    parser takes it as an input rather than detecting it from the resume
    (agents/resume_parser/agent.py has no "extract the candidate's name"
    step) - the frontend collects it before the upload, not after.

    Errors: 400 `invalid_request` for a non-PDF/DOCX file, an empty upload,
    an oversized upload, or a file with no extractable text (corrupted,
    password-protected, or a scanned image with no text layer); 503
    `dependency_unavailable` if even the parser's own deterministic
    fallback fails.
    """
    content = await resume_file.read()
    if not content:
        raise BadRequestError("The uploaded file is empty.")
    if len(content) > _MAX_RESUME_FILE_BYTES:
        raise BadRequestError(
            f"The uploaded resume is too large (max "
            f"{_MAX_RESUME_FILE_BYTES // (1024 * 1024)} MB)."
        )

    try:
        resume_text, source_format = extract_resume_text(resume_file.filename or "", content)
    except (UnsupportedResumeFormatError, ResumeExtractionError) as exc:
        raise BadRequestError(str(exc)) from exc

    parsed_resume, used_fallback, warning = await service.preview_resume(
        resume_text=resume_text, candidate_name=candidate_name,
    )
    parsed_resume = parsed_resume.model_copy(update={"source_format": source_format})
    return ParseResumeFileResponse(
        parsed_resume=parsed_resume,
        resume_text=resume_text,
        used_fallback=used_fallback,
        parse_warning=warning,
    )


@router.get("/{candidate_id}", response_model=CandidateResponse)
async def get_candidate(
    candidate_id: str,
    service: CandidateService = Depends(get_candidate_service),
    principal: Principal = Depends(require_authenticated),
) -> CandidateResponse:
    """GET /candidates/{candidate_id}. Errors: 404 `not_found`, 403 `forbidden`
    if the candidate does not belong to the authenticated user."""
    user_id = principal.subject_id or "user_anonymous"
    record = await service.validate_candidate_ownership(candidate_id, user_id)
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

    Errors: 404 `not_found`, 403 `forbidden` if the candidate does not belong
    to the authenticated user; 503 `dependency_unavailable` if a supplied
    `resume_text` fails to parse even via the fallback.
    """
    user_id = principal.subject_id or "user_anonymous"
    await service.validate_candidate_ownership(candidate_id, user_id)
    data = payload.model_dump(exclude_unset=True)
    resume_text = data.pop("resume_text", None)
    record = await service.update_candidate(candidate_id, resume_text=resume_text, patch=data)
    return CandidateResponse.from_record(record)
