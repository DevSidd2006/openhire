from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from schemas.live_interview import LiveTranscriptEvent
from services.live_transcript import project_live_transcript


def event(sequence: int, speaker: str, text: str, *, interrupted: bool = False):
    return LiveTranscriptEvent(
        event_id=f"evt_{sequence}",
        sequence=sequence,
        speaker=speaker,
        text=text,
        started_at_ms=sequence * 1_000,
        ended_at_ms=sequence * 1_000 + 500,
        interrupted=interrupted,
    )


def project(events):
    return project_live_transcript(
        interview_id="int_live_1",
        candidate_id="cand_1",
        job_id="job_1",
        started_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
        ended_at=datetime(2026, 10, 9, 0, 2, tzinfo=timezone.utc),
        events=events,
    )


def test_projector_pairs_interviewer_and_candidate_turns():
    transcript = project(
        [
            event(1, "interviewer", "Tell me about your Python work."),
            event(2, "candidate", "I built an async processing service."),
        ]
    )

    assert transcript.is_sealed is True
    assert transcript.interview_type == "gemini_live"
    question, answer = transcript.exchanges[0]
    assert question.question_id == "q_int_live_1_1"
    assert answer.answer_text == "I built an async processing service."
    assert answer.duration_seconds == 0.5


def test_projector_coalesces_fragmented_turns_in_sequence_order():
    transcript = project(
        [
            event(3, "candidate", "service."),
            event(1, "interviewer", "Describe an async"),
            event(2, "interviewer", "Python project."),
            event(4, "interviewer", "What changed?"),
            event(5, "candidate", "Latency improved."),
        ]
    )

    assert len(transcript.exchanges) == 2
    assert transcript.exchanges[0][0].question_text == (
        "Describe an async Python project."
    )
    assert transcript.exchanges[0][1].answer_text == "service."
    assert transcript.exchanges[1][1].answer_text == "Latency improved."


def test_projector_keeps_unpaired_and_interrupted_output_in_raw_transcript():
    transcript = project(
        [
            event(1, "candidate", "Can I clarify the role?"),
            event(2, "interviewer", "Could you explain", interrupted=True),
            event(3, "interviewer", "Of course. The role focuses on APIs."),
        ]
    )

    assert transcript.exchanges == []
    assert "[candidate] Can I clarify the role?" in transcript.raw_transcript
    assert "[interviewer interrupted] Could you explain" in transcript.raw_transcript
    assert "Of course. The role focuses on APIs." in transcript.raw_transcript


def test_projector_does_not_pair_interrupted_interviewer_fragment_with_candidate_answer():
    transcript = project(
        [
            event(1, "interviewer", "Could you tell me about", interrupted=True),
            event(2, "candidate", "Actually I wanted to say something first."),
            event(3, "interviewer", "Go ahead."),
            event(4, "candidate", "I have worked with distributed systems."),
        ]
    )

    assert len(transcript.exchanges) == 1
    assert transcript.exchanges[0][0].question_text == "Go ahead."
    assert transcript.exchanges[0][1].answer_text == "I have worked with distributed systems."
    assert "[interviewer interrupted] Could you tell me about" in transcript.raw_transcript


def test_event_text_is_normalized_and_blank_text_is_rejected():
    assert event(1, "candidate", "  clear\n answer  ").text == "clear answer"
    with pytest.raises(ValidationError):
        event(1, "candidate", " \n\t ")
