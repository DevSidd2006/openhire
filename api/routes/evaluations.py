"""
Evaluation retrieval endpoints.

Thin translators, same pattern as every other route module in this
backend: validate the request -> call exactly one `EvaluationService`
method -> shape the response. No agent orchestration, no aggregation, no
scoring logic lives here - all of that stays inside
services/evaluation_service.py and the seven existing evaluation agents it
calls.

Triggering evaluation is NOT a route here: it happens automatically at the
points a session becomes newly sealed - see api/routes/interview.py
(`submit_answer`, `finish_session`, `get_session_state`) and
api/routes/voice.py. Retrying a FAILED evaluation IS exposed here
(`POST /evaluations/{evaluation_id}/retry`) since Chunk 4 Step 6 explicitly
calls for a controlled (never automatic) retry path, and there is nowhere
more natural to put it than alongside the resource it acts on.
"""
from fastapi import APIRouter, Depends

from api.models_evaluations import EvaluationJobResponse
from core.dependencies import get_evaluation_service
from core.security import Principal, require_authenticated
from services.evaluation_service import EvaluationService

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


@router.get("/{evaluation_id}", response_model=EvaluationJobResponse)
async def get_evaluation(
    evaluation_id: str,
    service: EvaluationService = Depends(get_evaluation_service),
    principal: Principal = Depends(require_authenticated),
) -> EvaluationJobResponse:
    """GET /evaluations/{evaluation_id} - status, and the full
    `CandidateReport` result once COMPLETED. Errors: 404 `not_found`.

    `evaluation_id` (server-generated, unguessable) is the sole lookup key -
    the same capability-token access model `session_id` already uses
    throughout this API (api/registry.py); see this module's own docstring
    for why finer-grained authorization is not yet enforceable.
    """
    job = await service.get_evaluation(evaluation_id)
    return EvaluationJobResponse.from_domain(job)


@router.post("/{evaluation_id}/retry", response_model=EvaluationJobResponse)
async def retry_evaluation(
    evaluation_id: str,
    service: EvaluationService = Depends(get_evaluation_service),
    principal: Principal = Depends(require_authenticated),
) -> EvaluationJobResponse:
    """POST /evaluations/{evaluation_id}/retry - explicitly re-run a FAILED
    evaluation (Chunk 4 Step 6). Never automatic.

    Errors: 404 `not_found`; 409 `conflict` if the evaluation is not
    currently FAILED (retrying a PENDING/RUNNING job would duplicate
    in-flight work; retrying a COMPLETED one would discard a real result).
    """
    job = await service.retry_evaluation(evaluation_id)
    return EvaluationJobResponse.from_domain(job)
