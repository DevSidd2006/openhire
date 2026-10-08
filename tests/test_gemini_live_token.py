from datetime import datetime, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.fernet import Fernet

from core.errors import ConfigurationError, UnauthorizedError
from repositories.interfaces import (
    CredentialStatus,
    LLMCredentialRecord,
    SessionRecord,
)
from repositories.memory import InMemoryLLMCredentialRepository
from schemas.job import Competency, JobDescription
from schemas.live_interview import LiveInterviewState
from schemas.resume import ParsedResume
from schemas.rubric import AnchoredCompetency, JobRubric, RubricStatus
from services.gemini_live_token import (
    CONTROL_TOKEN_AUDIENCE,
    GeminiLiveTokenService,
)
from services.llm_credential_service import LLMCredentialService
from utils.interview_session import SessionStatus


_CONTROL_SECRET = "control-secret-that-is-at-least-thirty-two-bytes"


class FakeAsyncAuthTokens:
    def __init__(self, owner):
        self.owner = owner

    async def create(self, *, config):
        self.owner.last_config = config
        return SimpleNamespace(
            name=self.owner.name,
            expire_time="2026-10-09T00:20:00Z",
        )


class FakeAuthTokenClient:
    def __init__(self, name="ephemeral-token"):
        self.name = name
        self.last_config = None
        self.aio = SimpleNamespace(auth_tokens=FakeAsyncAuthTokens(self))


def _rubric() -> JobRubric:
    anchors = {level: f"Level {level}" for level in range(1, 6)}
    return JobRubric(
        rubric_id="rubric_1",
        job_id="job_1",
        version=1,
        status=RubricStatus.APPROVED,
        competencies=[
            AnchoredCompetency(
                name="Python", definition="Build reliable Python services", weight=0.4,
                anchors=anchors,
            ),
            AnchoredCompetency(
                name="APIs", definition="Design production APIs", weight=0.3,
                anchors=anchors,
            ),
            AnchoredCompetency(
                name="Collaboration", definition="Work effectively with peers", weight=0.3,
                anchors=anchors,
            ),
        ],
    )


def _live_record(*, resumption_handle: str | None = None) -> SessionRecord:
    job = JobDescription(
        job_id="job_1",
        title="Backend Engineer",
        description="Build reliable APIs.",
        competencies=[
            Competency(name="Python", weight=0.4, importance="critical"),
            Competency(name="APIs", weight=0.3, importance="high"),
            Competency(name="Collaboration", weight=0.3, importance="medium"),
        ],
    )
    resume = ParsedResume(
        candidate_id="cand_1",
        candidate_name="Candidate One",
        summary="Backend engineer",
        skills=["Python"],
    )
    return SessionRecord(
        session_id="sess_live_token",
        interview_id="int_live_token",
        candidate_id="cand_1",
        job_id="job_1",
        status=SessionStatus.CREATED,
        interview_mode="gemini_live",
        live_state=LiveInterviewState(
            interview_id="int_live_token",
            rubric_snapshot=_rubric(),
            resumption_handle=resumption_handle,
        ),
        job_description=job,
        parsed_resume=resume,
    )


def _service(fake, **overrides) -> GeminiLiveTokenService:
    values = {
        "system_api_key": "system-key",
        "model": "gemini-live-test",
        "client_factory": lambda _: fake,
        "control_secret": _CONTROL_SECRET,
    }
    values.update(overrides)
    return GeminiLiveTokenService(**values)


@pytest.mark.asyncio
async def test_issue_locks_model_audio_instructions_transcription_and_resumption():
    fake = FakeAuthTokenClient()
    service = _service(fake)

    issued = await service.issue(
        session_record=_live_record(), recruiter_user_id=None
    )

    request = fake.last_config
    config = request.live_connect_constraints.config
    assert issued.gemini_token == "ephemeral-token"
    assert request.uses == 1
    assert request.live_connect_constraints.model == "gemini-live-test"
    assert config.response_modalities == ["AUDIO"]
    assert config.input_audio_transcription is not None
    assert config.output_audio_transcription is not None
    assert config.session_resumption is not None
    assert config.realtime_input_config.automatic_activity_detection.silence_duration_ms == 900
    assert config.context_window_compression.sliding_window.target_tokens == 16_000
    assert "Do not reveal scores" in str(config.system_instruction)
    assert "Backend Engineer" in str(config.system_instruction)
    assert {declaration.name for declaration in config.tools[0].function_declarations} == {
        "report_competency_progress",
        "request_interview_completion",
    }

    claims = jwt.decode(
        issued.control_token,
        _CONTROL_SECRET,
        algorithms=["HS256"],
        audience=CONTROL_TOKEN_AUDIENCE,
    )
    assert claims["sid"] == "sess_live_token"
    assert claims["sub"] == "cand_1"
    assert claims["jti"]


@pytest.mark.asyncio
async def test_issue_rejects_when_no_system_or_matching_saved_credential():
    fake = FakeAuthTokenClient()
    service = _service(fake, system_api_key="")

    with pytest.raises(ConfigurationError, match="not configured"):
        await service.issue(
            session_record=_live_record(), recruiter_user_id="recruiter_1"
        )
    assert fake.last_config is None


@pytest.mark.asyncio
async def test_non_gemini_byok_is_not_used_for_gemini_token_minting():
    encryption_key = Fernet.generate_key()
    repo = InMemoryLLMCredentialRepository()
    await repo.save(
        LLMCredentialRecord(
            user_id="recruiter_1",
            provider="openai",
            encrypted_key=Fernet(encryption_key).encrypt(b"openai-secret"),
            key_hint="...cret",
            status=CredentialStatus.ACTIVE,
        )
    )
    credentials = LLMCredentialService(
        credential_repository=repo,
        encryption_key=encryption_key.decode(),
    )
    fake = FakeAuthTokenClient()
    seen_keys = []
    service = _service(
        fake,
        system_api_key="",
        credential_service=credentials,
        client_factory=lambda key: seen_keys.append(key) or fake,
    )

    with pytest.raises(ConfigurationError):
        await service.issue(
            session_record=_live_record(), recruiter_user_id="recruiter_1"
        )
    assert seen_keys == []


@pytest.mark.asyncio
async def test_active_gemini_byok_is_preferred_over_system_key():
    encryption_key = Fernet.generate_key()
    repo = InMemoryLLMCredentialRepository()
    await repo.save(
        LLMCredentialRecord(
            user_id="recruiter_1",
            provider="gemini",
            encrypted_key=Fernet(encryption_key).encrypt(b"gemini-user-key"),
            key_hint="...-key",
            status=CredentialStatus.ACTIVE,
        )
    )
    credentials = LLMCredentialService(
        credential_repository=repo,
        encryption_key=encryption_key.decode(),
    )
    fake = FakeAuthTokenClient()
    seen_keys = []
    service = _service(
        fake,
        credential_service=credentials,
        client_factory=lambda key: seen_keys.append(key) or fake,
    )

    await service.issue(
        session_record=_live_record(), recruiter_user_id="recruiter_1"
    )

    assert seen_keys == ["gemini-user-key"]


@pytest.mark.asyncio
async def test_resume_token_locks_the_stored_resumption_handle():
    fake = FakeAuthTokenClient()
    service = _service(fake)

    await service.issue(
        session_record=_live_record(resumption_handle="resume-handle-1"),
        recruiter_user_id=None,
        resume=True,
    )

    config = fake.last_config.live_connect_constraints.config
    assert config.session_resumption.handle == "resume-handle-1"


@pytest.mark.asyncio
async def test_fresh_token_does_not_restore_a_stale_handle():
    fake = FakeAuthTokenClient()
    service = _service(fake)

    await service.issue(
        session_record=_live_record(resumption_handle="resume-handle-1"),
        recruiter_user_id=None,
        resume=False,
    )

    config = fake.last_config.live_connect_constraints.config
    assert config.session_resumption.handle is None


def test_control_token_verification_rejects_a_different_session():
    fake = FakeAuthTokenClient()
    service = _service(fake)
    now = int(datetime.now(timezone.utc).timestamp())
    token = jwt.encode(
        {
            "aud": CONTROL_TOKEN_AUDIENCE,
            "sid": "sess_other",
            "sub": "cand_1",
            "iat": now,
            "exp": now + 60,
            "jti": "one-use-id",
        },
        _CONTROL_SECRET,
        algorithm="HS256",
    )

    with pytest.raises(UnauthorizedError):
        service.verify_control_token(token, "sess_live_token")
