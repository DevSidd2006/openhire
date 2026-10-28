"""Constrained Gemini Live token issuance for browser audio sessions.

Only short-lived, one-use auth tokens leave the backend. The permanent Gemini
API key is resolved into a local variable, used to mint the token, and never
stored, logged, or returned.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol
from uuid import uuid4

import jwt
from google import genai
from google.genai import types

from core.errors import ConfigurationError, UnauthorizedError
from repositories.interfaces import SessionRecord
from services.llm_credential_service import LLMCredentialService


GEMINI_LIVE_WEBSOCKET_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService."
    "BidiGenerateContentConstrained"
)
CONTROL_TOKEN_AUDIENCE = "openhire-live-control"
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "gemini_live_interviewer.md"


class _ClientFactory(Protocol):
    def __call__(self, api_key: str): ...


@dataclass(frozen=True)
class IssuedLiveToken:
    gemini_token: str
    control_token: str
    model: str
    websocket_url: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedAudioTestToken:
    gemini_token: str
    model: str
    websocket_url: str
    expires_at: datetime


progress_and_completion_tool = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="report_competency_progress",
            description="Report rubric coverage without assigning a score.",
            behavior="NON_BLOCKING",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "competency": {"type": "string"},
                    "evidence_state": {
                        "type": "string",
                        "enum": ["mentioned", "partial", "supported"],
                    },
                },
                "required": ["competency", "evidence_state"],
                "additionalProperties": False,
            },
        ),
        types.FunctionDeclaration(
            name="request_interview_completion",
            description="Ask OpenHire whether the interview may finish.",
            behavior="NON_BLOCKING",
            parameters_json_schema={
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
                "additionalProperties": False,
            },
        ),
    ]
)


def _default_client_factory(api_key: str):
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version="v1beta"),
    )


def _as_utc_datetime(value: str | datetime | None, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = fallback
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class GeminiLiveTokenService:
    """Mint locked Gemini and OpenHire control tokens for a live record."""

    def __init__(
        self,
        *,
        system_api_key: str,
        model: str,
        control_secret: str,
        voice: str = "Aoede",
        control_token_minutes: int = 20,
        hard_stop_seconds: int = 900,
        credential_service: LLMCredentialService | None = None,
        client_factory: _ClientFactory = _default_client_factory,
        websocket_url: str = GEMINI_LIVE_WEBSOCKET_URL,
    ) -> None:
        self._system_api_key = system_api_key
        self._model = model
        self._voice = voice
        self._control_secret = control_secret
        self._control_token_minutes = control_token_minutes
        self._hard_stop_seconds = hard_stop_seconds
        self._credentials = credential_service
        self._client_factory = client_factory
        self._websocket_url = websocket_url
        self._prompt_template = _PROMPT_PATH.read_text(encoding="utf-8")

    async def issue_audio_test(self) -> IssuedAudioTestToken:
        """Issue a one-use browser token for the development audio playground."""
        api_key = self._system_api_key or None
        if api_key is None:
            raise ConfigurationError("Realtime interviewing is not configured.")

        now = datetime.now(timezone.utc)
        requested_expires_at = now + timedelta(minutes=self._control_token_minutes)
        client = self._client_factory(api_key)
        auth_token = await client.aio.auth_tokens.create(
            config=types.CreateAuthTokenConfig(
                uses=1,
                expire_time=requested_expires_at.isoformat(),
                new_session_expire_time=(now + timedelta(minutes=1)).isoformat(),
                live_connect_constraints=types.LiveConnectConstraints(
                    model=self._model,
                    config=types.LiveConnectConfig(
                        response_modalities=["AUDIO"],
                        system_instruction=(
                            "You are a friendly realtime voice assistant in an audio "
                            "playground. Have a natural, concise, free-flowing conversation. "
                            "Respond to whatever the user says, ask follow-up questions when "
                            "useful, and allow the user to interrupt you at any time. Do not "
                            "conduct a job interview or refer to resumes, rubrics, or hiring."
                        ),
                        speech_config=types.SpeechConfig(
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                    voice_name=self._voice
                                )
                            )
                        ),
                        input_audio_transcription=types.AudioTranscriptionConfig(
                            mode="VERBATIM"
                        ),
                        output_audio_transcription=types.AudioTranscriptionConfig(),
                        realtime_input_config=types.RealtimeInputConfig(
                            automatic_activity_detection=types.AutomaticActivityDetection(
                                disabled=False,
                                prefix_padding_ms=20,
                                silence_duration_ms=700,
                            ),
                            activity_handling="START_OF_ACTIVITY_INTERRUPTS",
                        ),
                        context_window_compression=types.ContextWindowCompressionConfig(
                            sliding_window=types.SlidingWindow(target_tokens=16_000)
                        ),
                    ),
                ),
            )
        )
        token_name = getattr(auth_token, "name", None)
        if not token_name:
            raise ConfigurationError("Realtime audio test could not be initialized.")
        return IssuedAudioTestToken(
            gemini_token=token_name,
            model=self._model,
            websocket_url=self._websocket_url,
            expires_at=_as_utc_datetime(
                getattr(auth_token, "expire_time", None),
                fallback=requested_expires_at,
            ),
        )

    async def issue(
        self,
        *,
        session_record: SessionRecord,
        recruiter_user_id: str | None,
        resume: bool = False,
    ) -> IssuedLiveToken:
        """Issue one-use credentials whose Live setup is fixed server-side."""
        live_state = session_record.live_state
        if session_record.interview_mode != "gemini_live" or live_state is None:
            raise ConfigurationError(
                "Realtime credentials require a Gemini Live interview session."
            )
        if session_record.job_description is None or session_record.parsed_resume is None:
            raise ConfigurationError(
                "Realtime interview context is incomplete.",
                internal_detail=f"Live session {session_record.session_id!r} lacks job or resume context",
            )

        api_key: str | None = None
        if recruiter_user_id and self._credentials is not None:
            api_key = await self._credentials.resolve_api_key_for_provider(
                recruiter_user_id, "gemini"
            )
        api_key = api_key or self._system_api_key or None
        if api_key is None:
            raise ConfigurationError("Realtime interviewing is not configured.")

        resume_handle = live_state.resumption_handle if resume else None
        now = datetime.now(timezone.utc)
        requested_expires_at = now + timedelta(minutes=self._control_token_minutes)
        new_session_expires_at = now + timedelta(minutes=1)
        instruction = self._render_instruction(session_record, now=now)

        client = self._client_factory(api_key)
        auth_token = await client.aio.auth_tokens.create(
            config=types.CreateAuthTokenConfig(
                uses=1,
                expire_time=requested_expires_at.isoformat(),
                new_session_expire_time=new_session_expires_at.isoformat(),
                live_connect_constraints=types.LiveConnectConstraints(
                    model=self._model,
                    config=types.LiveConnectConfig(
                        response_modalities=["AUDIO"],
                        system_instruction=instruction,
                        speech_config=types.SpeechConfig(
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                    voice_name=self._voice
                                )
                            )
                        ),
                        input_audio_transcription=types.AudioTranscriptionConfig(
                            mode="VERBATIM"
                        ),
                        output_audio_transcription=types.AudioTranscriptionConfig(),
                        realtime_input_config=types.RealtimeInputConfig(
                            automatic_activity_detection=types.AutomaticActivityDetection(
                                disabled=False,
                                prefix_padding_ms=20,
                                silence_duration_ms=900,
                            ),
                            activity_handling="START_OF_ACTIVITY_INTERRUPTS",
                        ),
                        session_resumption=types.SessionResumptionConfig(
                            handle=resume_handle
                        ),
                        context_window_compression=types.ContextWindowCompressionConfig(
                            sliding_window=types.SlidingWindow(target_tokens=16_000)
                        ),
                        tools=[progress_and_completion_tool],
                    ),
                ),
            )
        )
        token_name = getattr(auth_token, "name", None)
        if not token_name:
            raise ConfigurationError(
                "Realtime interviewing could not be initialized.",
                internal_detail="Gemini auth-token response did not include a token name",
            )

        control_token = jwt.encode(
            {
                "aud": CONTROL_TOKEN_AUDIENCE,
                "sid": session_record.session_id,
                "sub": session_record.candidate_id,
                "iat": int(now.timestamp()),
                "exp": int(requested_expires_at.timestamp()),
                "jti": uuid4().hex,
            },
            self._control_secret,
            algorithm="HS256",
        )
        return IssuedLiveToken(
            gemini_token=token_name,
            control_token=control_token,
            model=self._model,
            websocket_url=self._websocket_url,
            expires_at=_as_utc_datetime(
                getattr(auth_token, "expire_time", None),
                fallback=requested_expires_at,
            ),
        )

    def verify_control_token(self, token: str, session_id: str) -> str:
        """Validate the narrow control-socket token and return its subject."""
        try:
            claims = jwt.decode(
                token,
                self._control_secret,
                algorithms=["HS256"],
                audience=CONTROL_TOKEN_AUDIENCE,
                options={"require": ["aud", "sid", "sub", "iat", "exp", "jti"]},
            )
        except jwt.InvalidTokenError as exc:
            raise UnauthorizedError("The realtime control token is invalid or expired.") from exc
        if claims.get("sid") != session_id:
            raise UnauthorizedError("The realtime control token does not match this session.")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise UnauthorizedError("The realtime control token has no valid subject.")
        return subject

    def _render_instruction(self, session_record: SessionRecord, *, now: datetime) -> str:
        live_state = session_record.live_state
        assert live_state is not None
        assert session_record.job_description is not None
        assert session_record.parsed_resume is not None

        remaining_seconds = self._hard_stop_seconds
        if live_state.hard_stop_at is not None:
            hard_stop = live_state.hard_stop_at
            if hard_stop.tzinfo is None:
                hard_stop = hard_stop.replace(tzinfo=timezone.utc)
            remaining_seconds = max(60, int((hard_stop - now).total_seconds()))
        duration_minutes = max(1, math.ceil(remaining_seconds / 60))

        return self._prompt_template.format(
            duration_minutes=duration_minutes,
            role_title=session_record.job_description.title,
            job_description=json.dumps(
                session_record.job_description.model_dump(mode="json"),
                indent=2,
                sort_keys=True,
            ),
            rubric=json.dumps(
                live_state.rubric_snapshot.model_dump(mode="json"),
                indent=2,
                sort_keys=True,
            ),
            resume=json.dumps(
                session_record.parsed_resume.model_dump(mode="json"),
                indent=2,
                sort_keys=True,
            ),
        )


__all__ = [
    "CONTROL_TOKEN_AUDIENCE",
    "GEMINI_LIVE_WEBSOCKET_URL",
    "GeminiLiveTokenService",
    "IssuedAudioTestToken",
    "IssuedLiveToken",
    "progress_and_completion_tool",
]
