"""Projection of finalized realtime events into the sealed transcript model."""
from __future__ import annotations

from datetime import datetime

from schemas.interview import InterviewAnswer, InterviewQuestion, InterviewTranscript
from schemas.live_interview import LiveTranscriptEvent


def _line(event: LiveTranscriptEvent) -> str:
    suffix = " interrupted" if event.interrupted else ""
    return f"[{event.speaker}{suffix}] {event.text}"


def project_live_transcript(
    *,
    interview_id: str,
    candidate_id: str,
    job_id: str,
    started_at: datetime,
    ended_at: datetime,
    events: list[LiveTranscriptEvent],
) -> InterviewTranscript:
    """Build the evaluation pipeline's transcript without dropping raw events.

    Consecutive finalized fragments from the same conversational side are
    coalesced.  Only interviewer content followed by candidate content forms
    an exchange; greetings, interrupted output, and unpaired fragments remain
    available in ``raw_transcript`` for audit and display.
    """

    ordered = sorted(events, key=lambda item: item.sequence)
    exchanges: list[tuple[InterviewQuestion, InterviewAnswer]] = []
    pending_interviewer: list[LiveTranscriptEvent] = []
    pending_candidate: list[LiveTranscriptEvent] = []

    def flush_pair() -> None:
        if not pending_interviewer or not pending_candidate:
            return
        first_question = pending_interviewer[0]
        question = InterviewQuestion(
            question_id=f"q_{interview_id}_{first_question.sequence}",
            question_text=" ".join(item.text for item in pending_interviewer),
            category="role_specific",
            question_type="follow_up",
        )
        answer = InterviewAnswer(
            question_id=question.question_id,
            answer_text=" ".join(item.text for item in pending_candidate),
            timestamp_start=pending_candidate[0].started_at_ms / 1000,
            timestamp_end=pending_candidate[-1].ended_at_ms / 1000,
            duration_seconds=max(
                0,
                (
                    pending_candidate[-1].ended_at_ms
                    - pending_candidate[0].started_at_ms
                )
                / 1000,
            ),
        )
        exchanges.append((question, answer))
        pending_interviewer.clear()
        pending_candidate.clear()

    for item in ordered:
        if item.speaker == "interviewer":
            flush_pair()
            pending_interviewer.append(item)
        elif pending_interviewer:
            pending_candidate.append(item)
    flush_pair()

    return InterviewTranscript(
        interview_id=interview_id,
        candidate_id=candidate_id,
        job_id=job_id,
        start_time=started_at.isoformat(),
        end_time=ended_at.isoformat(),
        duration_seconds=max(0, int((ended_at - started_at).total_seconds())),
        exchanges=exchanges,
        interviewer_name="OpenHire AI Interviewer",
        interview_type="gemini_live",
        format="voice",
        is_sealed=True,
        seal_timestamp=ended_at.isoformat(),
        raw_transcript="\n".join(_line(item) for item in ordered),
    )
