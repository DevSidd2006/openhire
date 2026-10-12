"""Domain models for a browser-relayed Gemini Live interview.

Partial transcript events live in :class:`LiveInterviewState` until the
session is finished.  Only then are they projected into the existing sealed
``InterviewTranscript`` representation.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from schemas.rubric import JobRubric


class LiveInterviewStatus(str, Enum):
    CREATED = "created"
    CONNECTING = "connecting"
    ACTIVE = "active"
    RECONNECTING = "reconnecting"
    FINISHING = "finishing"
    SEALED = "sealed"
    FAILED = "failed"


class LiveTranscriptEvent(BaseModel):
    """One finalized transcript fragment accepted from the control client."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    speaker: Literal["candidate", "interviewer"]
    text: str = Field(min_length=1, max_length=20_000)
    started_at_ms: int = Field(ge=0)
    ended_at_ms: int = Field(ge=0)
    gemini_turn_id: Optional[str] = Field(default=None, max_length=256)
    interrupted: bool = False

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("text must not be blank")
        return normalized


class LiveLatencyMetric(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: Literal[
        "connection_setup", "turn_to_first_audio", "interruption_stop", "reconnect"
    ]
    duration_ms: float = Field(ge=0, le=300_000)
    turn_id: Optional[str] = Field(default=None, max_length=256)


class CompetencyProgress(BaseModel):
    model_config = ConfigDict(frozen=True)

    competency: str = Field(min_length=1, max_length=200)
    evidence_state: Literal["mentioned", "partial", "supported"]

    @field_validator("competency")
    @classmethod
    def normalize_competency(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("competency must not be blank")
        return normalized


class CompletionDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    approved: bool
    remaining_competencies: list[str] = Field(default_factory=list)
    reason: str


class LiveInterviewState(BaseModel):
    """Durable, append-only state for one realtime interview."""

    model_config = ConfigDict(frozen=True)

    interview_id: str
    rubric_snapshot: JobRubric
    status: LiveInterviewStatus = LiveInterviewStatus.CREATED
    started_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    wrap_up_at: Optional[datetime] = None
    hard_stop_at: Optional[datetime] = None
    last_sequence: int = 0
    events: list[LiveTranscriptEvent] = Field(default_factory=list)
    resumption_handle: Optional[str] = None
    reconnect_count: int = 0
    competency_progress: list[CompetencyProgress] = Field(default_factory=list)
    metrics: list[LiveLatencyMetric] = Field(default_factory=list)
    termination_reason: Optional[str] = None
    failure_category: Optional[str] = None
    source: Literal["gemini_live_client_relay"] = "gemini_live_client_relay"


__all__ = [
    "CompetencyProgress",
    "CompletionDecision",
    "LiveInterviewState",
    "LiveInterviewStatus",
    "LiveLatencyMetric",
    "LiveTranscriptEvent",
]
