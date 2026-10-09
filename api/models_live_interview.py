"""Transport models for Gemini Live interview sessions.

The durable lifecycle and transcript event models live in
``schemas.live_interview``.  This module only defines the narrow HTTP and
control-WebSocket representations exposed to a browser.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from repositories.interfaces import EvaluationStatus
from schemas.live_interview import (
    CompetencyProgress,
    LiveInterviewStatus,
    LiveLatencyMetric,
    LiveTranscriptEvent,
)


class LiveSessionResponse(BaseModel):
    session_id: str
    interview_id: str
    status: LiveInterviewStatus
    interview_mode: Literal["gemini_live"] = "gemini_live"
    application_id: str | None = None
    last_sequence: int = 0
    wrap_up_at: datetime | None = None
    hard_stop_at: datetime | None = None
    reconnect_after_seconds: int = 540


class LiveTokenRequest(BaseModel):
    resume: bool = False


class LiveTokenResponse(BaseModel):
    gemini_token: str
    control_token: str
    model: str
    websocket_url: str
    expires_at: datetime


class LiveAudioTestTokenResponse(BaseModel):
    gemini_token: str
    model: str
    websocket_url: str
    expires_at: datetime


class LiveFinishRequest(BaseModel):
    pending_events: list[LiveTranscriptEvent] = Field(default_factory=list)


class LiveFinishResponse(BaseModel):
    session_id: str
    status: LiveInterviewStatus
    transcript_persisted: bool
    evaluation_id: str | None = None
    evaluation_status: EvaluationStatus | None = None


class ControlAuthenticate(BaseModel):
    type: Literal["authenticate"]
    token: str = Field(min_length=1, max_length=4096)


class ControlTranscriptFinal(BaseModel):
    type: Literal["transcript_final"]
    event: LiveTranscriptEvent


class ControlProgress(BaseModel):
    type: Literal["competency_progress"]
    call_id: str = Field(min_length=1, max_length=256)
    progress: CompetencyProgress


class ControlMetric(BaseModel):
    type: Literal["latency_metric"]
    metric: LiveLatencyMetric


class ControlResumptionHandle(BaseModel):
    type: Literal["resumption_handle"]
    handle: str = Field(min_length=1, max_length=8192)


class ControlLifecycle(BaseModel):
    type: Literal["started", "reconnecting", "complete", "failed"]
    reason: str | None = Field(default=None, max_length=500)


class ControlCompletionRequest(BaseModel):
    type: Literal["completion_request"]
    call_id: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=500)


CONTROL_MESSAGE_MODELS = {
    "authenticate": ControlAuthenticate,
    "transcript_final": ControlTranscriptFinal,
    "competency_progress": ControlProgress,
    "latency_metric": ControlMetric,
    "resumption_handle": ControlResumptionHandle,
    "completion_request": ControlCompletionRequest,
    "started": ControlLifecycle,
    "reconnecting": ControlLifecycle,
    "complete": ControlLifecycle,
    "failed": ControlLifecycle,
}


__all__ = [
    "CONTROL_MESSAGE_MODELS",
    "ControlAuthenticate",
    "ControlCompletionRequest",
    "ControlLifecycle",
    "ControlMetric",
    "ControlProgress",
    "ControlResumptionHandle",
    "ControlTranscriptFinal",
    "LiveFinishRequest",
    "LiveFinishResponse",
    "LiveAudioTestTokenResponse",
    "LiveSessionResponse",
    "LiveTokenRequest",
    "LiveTokenResponse",
]
