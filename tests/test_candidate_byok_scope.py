from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.dependencies import resolve_candidate_practice_llm_provider
from core.config import AppSettings
from core.security import Principal, PrincipalType
from core.llm_context import get_context_provider
from fastapi import WebSocketDisconnect
from utils.interview_session import SessionStatus
from utils.voice_turn import VoiceTurnOutcome, VoiceTurnResult

_KEY = "IcVLp2xXjBdCQ8f5T7QYqGzq0m5ZKq8P4o5r5tqXQvI="


@pytest.mark.asyncio
async def test_candidate_byok_resolves_only_for_owned_practice_jobs(monkeypatch):
    provider = object()
    resolved = AsyncMock(return_value=provider)
    monkeypatch.setattr("services.llm_credential_service.LLMCredentialService.resolve_provider_for_user", resolved)
    jobs = SimpleNamespace(get_job=AsyncMock(return_value=SimpleNamespace(is_practice=True, created_by_user_id="candidate-user")))
    candidates = SimpleNamespace(validate_candidate_ownership=AsyncMock())
    container = SimpleNamespace(llm_credential_repository=object())
    settings = AppSettings(auth_enabled=True, byok_encryption_key=_KEY)
    candidate = Principal(principal_type=PrincipalType.CANDIDATE, subject_id="candidate-user")

    result = await resolve_candidate_practice_llm_provider(
        principal=candidate, job_id="practice-job", candidate_id="candidate-profile",
        application_id=None, settings=settings, container=container,
        jobs=jobs, candidate_service=candidates,
    )
    assert result is provider
    candidates.validate_candidate_ownership.assert_awaited_once_with("candidate-profile", "candidate-user")
    resolved.assert_awaited_once_with("candidate-user")


@pytest.mark.asyncio
@pytest.mark.parametrize("practice,owner,application_id", [
    (False, "candidate-user", None),
    (True, "another-user", None),
    (True, "candidate-user", "real-application"),
])
async def test_candidate_byok_is_not_resolved_for_real_or_other_users_jobs(monkeypatch, practice, owner, application_id):
    resolved = AsyncMock()
    monkeypatch.setattr("services.llm_credential_service.LLMCredentialService.resolve_provider_for_user", resolved)
    jobs = SimpleNamespace(get_job=AsyncMock(return_value=SimpleNamespace(is_practice=practice, created_by_user_id=owner)))
    candidates = SimpleNamespace(validate_candidate_ownership=AsyncMock())
    candidate = Principal(principal_type=PrincipalType.CANDIDATE, subject_id="candidate-user")

    result = await resolve_candidate_practice_llm_provider(
        principal=candidate, job_id="job", candidate_id="profile",
        application_id=application_id, settings=AppSettings(auth_enabled=True, byok_encryption_key=_KEY),
        container=SimpleNamespace(llm_credential_repository=object()), jobs=jobs,
        candidate_service=candidates,
    )
    assert result is None
    resolved.assert_not_awaited()
    candidates.validate_candidate_ownership.assert_not_awaited()


@pytest.mark.asyncio
async def test_candidate_voice_turn_uses_its_byok_provider_and_resets_context(monkeypatch):
    from api.routes import voice as route

    provider = object()
    candidate = Principal(principal_type=PrincipalType.CANDIDATE, subject_id="candidate-user")
    runner = SimpleNamespace(job_description=SimpleNamespace(job_id="practice-job"),
                             candidate_id="profile", status=SessionStatus.ACTIVE)
    async def get_runner(_session_id):
        assert get_context_provider() is provider
        return runner

    interview = SimpleNamespace(
        get_runner=get_runner,
        get_record=AsyncMock(return_value=SimpleNamespace(
            job_id="practice-job", candidate_id="profile", application_id=None)),
        record_turn_outcome=AsyncMock(),
    )

    async def submit_text(_runner, _answer, **_kwargs):
        assert get_context_provider() is provider
        return VoiceTurnResult(outcome=VoiceTurnOutcome.ANSWER_RECORDED,
                               transcript="My answer", session_status=SessionStatus.ACTIVE)

    voice = SimpleNamespace(submit_text_answer=submit_text)
    container = SimpleNamespace(settings=AppSettings(auth_enabled=True, byok_encryption_key=_KEY))
    websocket = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(container=container)))
    messages = iter([
        {"type": "authenticate", "token": "candidate-token"},
        {"type": "answer_text", "transcript": "My answer"},
    ])
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock()
    websocket.close = AsyncMock()

    async def receive_json():
        try:
            return next(messages)
        except StopIteration:
            raise WebSocketDisconnect()

    websocket.receive_json = receive_json
    monkeypatch.setattr(route, "build_interview_service", lambda _app: interview)
    monkeypatch.setattr(route, "build_evaluation_service", lambda _app: object())
    monkeypatch.setattr(route, "build_voice_service", lambda _app: voice)
    monkeypatch.setattr(route, "get_auth_service", lambda _ws: SimpleNamespace(
        verify_access_token=AsyncMock(return_value=candidate)))
    monkeypatch.setattr(route, "get_job_service", lambda _ws: object())
    monkeypatch.setattr(route, "get_candidate_service", lambda _ws: object())
    resolve = AsyncMock(return_value=provider)
    monkeypatch.setattr(route, "resolve_candidate_practice_llm_provider", resolve)

    await route.voice_socket(websocket, "session")

    resolve.assert_awaited_once()
    interview.record_turn_outcome.assert_awaited_once_with("session", runner)
    assert get_context_provider() is None
    assert websocket.send_json.await_args_list[-1].args[0]["type"] == "turn_result"
