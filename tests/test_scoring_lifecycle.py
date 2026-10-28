"""
Scoring lifecycle validation.

Re-initializing a session must not erase accumulated answers, and scoring an
unknown session must be reported as missing rather than represented by a
fabricated zero score.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.routes import scoring as scoring_routes
from api.routes.scoring import get_scoring_service
from core.config import AppSettings
from core.dependencies import get_app_settings
from core.errors import ConflictError, DependencyError, NotFoundError
from core.security import Principal, PrincipalType, require_authenticated
from repositories.interfaces import CandidateRecord, SessionRecord
from repositories.memory import InMemoryCandidateRepository, InMemorySessionRepository
from schemas.resume import ParsedResume
from services.background_scoring import (
    AnswerScore,
    BackgroundScoringService,
    InterviewScoring,
)
from utils.interview_session import SessionStatus


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_client():
    app = create_app()
    current = {
        "principal": Principal(
            principal_type=PrincipalType.CANDIDATE,
            subject_id="user_owner",
        )
    }
    settings = AppSettings(auth_enabled=True)
    app.dependency_overrides[require_authenticated] = lambda: current["principal"]
    app.dependency_overrides[get_app_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client, current


def _seed_session(
    client: TestClient,
    session_id: str,
    *,
    candidate_id: str = "cand_http_lifecycle",
    job_id: str = "job_http_lifecycle",
) -> None:
    record = SessionRecord(
        session_id=session_id,
        candidate_id=candidate_id,
        job_id=job_id,
        status=SessionStatus.CREATED,
    )
    asyncio.run(client.app.state.container.session_repository.save(record))


def _initialize_payload(
    session_id: str,
    *,
    candidate_id: str = "cand_http_lifecycle",
    job_id: str = "job_http_lifecycle",
) -> dict:
    return {
        "session_id": session_id,
        "candidate_id": candidate_id,
        "job_id": job_id,
    }


@pytest.mark.asyncio
async def test_reinitializing_a_session_preserves_accumulated_answers():
    service = BackgroundScoringService()
    scoring = await service.initialize_session(
        session_id="session_lifecycle",
        candidate_id="cand_lifecycle",
        job_id="job_lifecycle",
    )
    scoring.add_answer(
        AnswerScore(
            question_id="q1",
            question_text="Question",
            answer_text="Answer",
            competency="Python",
            clarity=8,
            completeness=8,
            relevance=8,
        )
    )

    again = await service.initialize_session(
        session_id="session_lifecycle",
        candidate_id="cand_lifecycle",
        job_id="job_lifecycle",
    )

    assert again is scoring
    assert len(again.answers) == 1


@pytest.mark.asyncio
async def test_reinitializing_with_a_different_candidate_is_a_conflict():
    service = BackgroundScoringService()
    await service.initialize_session(
        session_id="session_lifecycle",
        candidate_id="cand_first",
        job_id="job_lifecycle",
    )

    with pytest.raises(ConflictError):
        await service.initialize_session(
            session_id="session_lifecycle",
            candidate_id="cand_second",
            job_id="job_lifecycle",
        )


@pytest.mark.asyncio
async def test_scoring_an_unknown_session_raises_not_found():
    service = BackgroundScoringService()

    with pytest.raises(NotFoundError):
        await service.score_answer(
            session_id="session_never_initialized",
            question_id="q1",
            question_text="Question",
            answer_text="Answer",
            competency="Python",
            candidate_name="Jane Doe",
        )


def test_score_answer_for_an_unknown_session_returns_404(client):
    response = client.post(
        "/scoring/score-answer",
        json={
            "session_id": "session_never_initialized",
            "question_id": "q1",
            "question_text": "Question",
            "answer_text": "Answer",
            "competency": "Python",
            "candidate_name": "Jane Doe",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_reinitializing_through_http_is_idempotent(client):
    _seed_session(client, "session_http_lifecycle")
    payload = _initialize_payload("session_http_lifecycle")

    first = client.post("/scoring/initialize", json=payload)
    second = client.post("/scoring/initialize", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "initialized"


def test_conflicting_initialization_through_http_is_409(client):
    _seed_session(client, "session_http_conflict")
    first = client.post(
        "/scoring/initialize", json=_initialize_payload("session_http_conflict")
    )
    assert first.status_code == 200

    conflict = client.post(
        "/scoring/initialize",
        json=_initialize_payload(
            "session_http_conflict",
            candidate_id="cand_other",
            job_id="job_other",
        ),
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"] == "conflict"


def test_initialization_requires_an_existing_interview_session(client):
    response = client.post(
        "/scoring/initialize", json=_initialize_payload("session_no_interview")
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_provider_failure_raises_instead_of_returning_zero_scores():
    class _BrokenMediator:
        async def analyze_response(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    service = BackgroundScoringService()
    await service.initialize_session(
        session_id="session_provider_failure",
        candidate_id="cand_provider_failure",
        job_id="job_provider_failure",
    )
    service.mediator_agent = _BrokenMediator()

    with pytest.raises(DependencyError):
        await service.score_answer(
            session_id="session_provider_failure",
            question_id="q1",
            question_text="Question",
            answer_text="Answer",
            competency="Python",
            candidate_name="Jane Doe",
        )

    scoring = await service.get_session_scores("session_provider_failure")
    assert scoring is not None
    assert scoring.answers == []


def test_provider_failure_through_http_is_503(client):
    class _BrokenMediator:
        async def analyze_response(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    service = BackgroundScoringService()
    service.scores["session_provider_failure_http"] = InterviewScoring(
        session_id="session_provider_failure_http",
        candidate_id="candidate_provider_failure_http",
        job_id="job_provider_failure_http",
    )
    service.mediator_agent = _BrokenMediator()
    client.app.dependency_overrides[get_scoring_service] = lambda: service
    _seed_session(
        client, "session_provider_failure_http",
        candidate_id="candidate_provider_failure_http",
        job_id="job_provider_failure_http",
    )

    response = client.post(
        "/scoring/score-answer",
        json={
            "session_id": "session_provider_failure_http",
            "question_id": "q1",
            "question_text": "Question",
            "answer_text": "Answer",
            "competency": "Python",
            "candidate_name": "Jane Doe",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"] == "dependency_unavailable"


def test_initialization_requires_candidate_ownership_when_auth_is_enabled(auth_client):
    client, current = auth_client
    owner_candidate = client.post(
        "/candidates",
        json={"resume_text": "Owner Python engineer", "candidate_name": "Owner"},
    ).json()["candidate_id"]
    _seed_session(
        client, "session_owned", candidate_id=owner_candidate, job_id="job_owned"
    )
    payload = _initialize_payload(
        "session_owned", candidate_id=owner_candidate, job_id="job_owned"
    )

    assert client.post("/scoring/initialize", json=payload).status_code == 200

    current["principal"] = Principal(
        principal_type=PrincipalType.CANDIDATE, subject_id="user_other"
    )
    hidden = client.post("/scoring/initialize", json=payload)
    assert hidden.status_code == 404

    current["principal"] = Principal(
        principal_type=PrincipalType.RECRUITER, subject_id="user_recruiter"
    )
    assert client.post("/scoring/initialize", json=payload).status_code == 200

    current["principal"] = Principal(
        principal_type=PrincipalType.CANDIDATE,
        subject_id="user_other",
        session_id="session_owned",
    )
    assert client.post("/scoring/initialize", json=payload).status_code == 200


@pytest.mark.asyncio
async def test_initialization_hides_unowned_session_before_pair_validation():
    sessions = InMemorySessionRepository()
    candidates = InMemoryCandidateRepository()
    service = BackgroundScoringService()
    settings = AppSettings(auth_enabled=True)
    await sessions.save(SessionRecord(
        session_id="session_private", candidate_id="cand_private",
        job_id="job_private", status=SessionStatus.CREATED,
        application_id="application_private",
    ))
    await candidates.save(CandidateRecord(
        candidate_id="cand_private", user_id="user_owner",
        resume=ParsedResume(candidate_id="cand_private", candidate_name="Owner"),
    ))
    outsider = Principal(principal_type=PrincipalType.CANDIDATE, subject_id="user_other")
    owner = Principal(principal_type=PrincipalType.CANDIDATE, subject_id="user_owner")

    async def initialize(session_id, candidate_id, job_id, principal):
        return await scoring_routes.initialize_scoring(
            request=scoring_routes.InitializeScoringRequest(
                session_id=session_id, candidate_id=candidate_id, job_id=job_id,
            ),
            service=service, sessions=sessions, candidates=candidates,
            principal=principal, settings=settings,
        )

    with pytest.raises(NotFoundError) as missing:
        await initialize("session_missing", "cand_private", "job_private", outsider)
    with pytest.raises(NotFoundError) as unowned:
        await initialize("session_private", "cand_wrong", "job_wrong", outsider)
    assert unowned.value.to_body() == missing.value.to_body()

    with pytest.raises(ConflictError):
        await initialize("session_private", "cand_wrong", "job_wrong", owner)
    assert (await initialize("session_private", "cand_private", "job_private", owner))["status"] == "initialized"


@pytest.mark.asyncio
async def test_legacy_session_without_candidate_record_can_initialize():
    sessions = InMemorySessionRepository()
    candidates = InMemoryCandidateRepository()
    service = BackgroundScoringService()
    principal = Principal(principal_type=PrincipalType.CANDIDATE, subject_id="user_legacy")
    settings = AppSettings(auth_enabled=True)
    await sessions.save(SessionRecord(
        session_id="session_legacy", candidate_id="cand_legacy",
        job_id="job_legacy", status=SessionStatus.CREATED,
    ))

    result = await scoring_routes.initialize_scoring(
        request=scoring_routes.InitializeScoringRequest(
            session_id="session_legacy", candidate_id="cand_legacy", job_id="job_legacy",
        ),
        service=service, sessions=sessions, candidates=candidates,
        principal=principal, settings=settings,
    )
    assert result["status"] == "initialized"

    await sessions.save(SessionRecord(
        session_id="session_linked", candidate_id="cand_legacy",
        job_id="job_legacy", status=SessionStatus.CREATED,
        application_id="application_linked",
    ))
    with pytest.raises(NotFoundError):
        await scoring_routes.initialize_scoring(
            request=scoring_routes.InitializeScoringRequest(
                session_id="session_linked", candidate_id="cand_legacy", job_id="job_legacy",
            ),
            service=service, sessions=sessions, candidates=candidates,
            principal=principal, settings=settings,
        )


@pytest.mark.asyncio
async def test_unowned_session_cannot_be_scored_read_finalized_or_cleared():
    sessions = InMemorySessionRepository()
    candidates = InMemoryCandidateRepository()
    service = BackgroundScoringService()
    settings = AppSettings(auth_enabled=True)
    await sessions.save(SessionRecord(
        session_id="session_owned", candidate_id="cand_owned",
        job_id="job_owned", status=SessionStatus.CREATED,
        application_id="application_owned",
    ))
    await candidates.save(CandidateRecord(
        candidate_id="cand_owned", user_id="user_owner",
        resume=ParsedResume(candidate_id="cand_owned", candidate_name="Owner"),
    ))
    await service.initialize_session("session_owned", "cand_owned", "job_owned")
    outsider = Principal(principal_type=PrincipalType.CANDIDATE, subject_id="user_other")
    kwargs = dict(
        service=service, sessions=sessions, candidates=candidates,
        principal=outsider, settings=settings,
    )

    class _BrokenMediator:
        async def analyze_response(self, **_kwargs):
            raise AssertionError("Unauthorized scoring reached the mediator")

    service.mediator_agent = _BrokenMediator()
    operations = (
        lambda: scoring_routes.score_answer(
            request=scoring_routes.ScoreAnswerRequest(
                session_id="session_owned", question_id="q1", question_text="Question",
                answer_text="Answer", competency="Python", candidate_name="Owner",
            ), **kwargs,
        ),
        lambda: scoring_routes.get_session_summary(session_id="session_owned", **kwargs),
        lambda: scoring_routes.get_session_scores(session_id="session_owned", **kwargs),
        lambda: scoring_routes.finalize_scoring(session_id="session_owned", **kwargs),
        lambda: scoring_routes.clear_session_scores(session_id="session_owned", **kwargs),
    )
    for operation in operations:
        with pytest.raises(NotFoundError):
            await operation()

    assert await service.get_session_scores("session_owned") is not None

    class _WorkingMediator:
        async def analyze_response(self, **_kwargs):
            return SimpleNamespace(
                clarity=8, completeness=9, relevance=7,
                areas_for_improvement=[],
            )

    service.mediator_agent = _WorkingMediator()
    owner_kwargs = {**kwargs, "principal": Principal(
        principal_type=PrincipalType.CANDIDATE, subject_id="user_owner",
    )}
    answer = await scoring_routes.score_answer(
        request=scoring_routes.ScoreAnswerRequest(
            session_id="session_owned", question_id="q1", question_text="Question",
            answer_text="Answer", competency="Python", candidate_name="Owner",
        ), **owner_kwargs,
    )
    assert answer["score"]["clarity"] == 8
    assert (await scoring_routes.get_session_summary(
        session_id="session_owned", **owner_kwargs,
    ))["total_answers"] == 1
    assert (await scoring_routes.get_session_scores(
        session_id="session_owned", **owner_kwargs,
    ))["total_answers"] == 1
    assert (await scoring_routes.finalize_scoring(
        session_id="session_owned", **owner_kwargs,
    ))["total_answers"] == 1
    assert (await scoring_routes.clear_session_scores(
        session_id="session_owned", **owner_kwargs,
    ))["status"] == "cleared"
    assert await service.get_session_scores("session_owned") is None


def test_scoring_routes_require_authentication():
    for route in scoring_routes.router.routes:
        assert require_authenticated in {
            dependency.call for dependency in route.dependant.dependencies
        }
