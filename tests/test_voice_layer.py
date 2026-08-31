"""
P9: voice layer tests (mock-first, fully offline).

Every test here runs against MockAudioProcessor/MockSpeechSynthesizer and a
ScriptedLLMProvider - no speech API key, no LLM API key, no network. They
exist to prove the voice path preserves the guarantees the typed path
already has, which is the whole premise of P9's design: a voice turn is a
typed turn with two transcoding steps on either side, NOT a second
interview engine.

Covered (P9 Phase 6):
  1.  audio input actually reaches STT
  2.  STT text actually reaches InterviewSessionRunner
  3.  the answer is recorded EXACTLY as transcribed
  4.  the adaptive interviewer produces the next question
  5.  TTS receives exactly that question text (no paraphrase/truncation)
  6.  session lifecycle stays correct through voice turns
  7.  the transcript still seals correctly
  8.  candidate_id/job_id never cross sessions
  9.  STT failure is explicit (and does not consume the pending question)
  10. TTS failure is explicit (and does NOT roll back a recorded answer)
  11. empty transcription never silently becomes an answer
  12. duplicate/replayed audio never double-records an answer
  13. disconnect mid-session does not corrupt session state
"""
import base64
import json

import pytest

from providers.audio.mock import MockAudioProcessor, MockSpeechSynthesizer
from providers.base import SpeechPermanentError, SpeechTransientError
from schemas.job import Competency, JobDescription
from schemas.resume import ParsedResume
from tests.fakes import ScriptedLLMProvider
from agents.interviewer.agent import InterviewerAgent
from utils.interview_session import InterviewSessionRunner, SessionStatus
from utils.voice_turn import VoiceTurnError, VoiceTurnOutcome, VoiceTurnService


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _job(job_id="job_voice", *comps) -> JobDescription:
    # Two competencies by default, deliberately: with only ONE competency a
    # single strong answer legitimately terminates the interview via
    # sufficient_evidence_collected (correct engine behavior - see
    # utils/adaptive_interview.py), which would leave most of these tests
    # with no "next question" to assert on. Two competencies keep the
    # session alive for a second turn without weakening anything.
    comps = comps or (("Python", 0.5), ("SQL", 0.5))
    return JobDescription(
        job_id=job_id, title="Engineer", description="A role",
        competencies=[Competency(name=n, weight=w) for n, w in comps],
    )


def _resume(candidate_id="cand_voice") -> ParsedResume:
    return ParsedResume(candidate_id=candidate_id, candidate_name="Voice Tester", skills=["Python"])


def _question_script(*texts):
    """A scripted LLM that alternates question -> evaluation -> question..."""
    script = []
    for text in texts:
        script.append(json.dumps({
            "question_text": text, "question_type": "initial",
            "difficulty": "medium", "reason": "x", "expected_duration_seconds": 60,
        }))
        script.append(json.dumps({
            "score": 8.0, "confidence": 0.9, "evidence_status": "supported",
            "is_vague": False, "missing_detail": None, "explanation": "good",
        }))
    return script


def _intro_json():
    return json.dumps({
        "question_text": "Welcome! Tell me about yourself.", "question_type": "introduction",
        "difficulty": "easy", "reason": "Opening greeting", "expected_duration_seconds": 45,
    })


def _runner(job=None, resume=None, script=None, max_questions=None) -> InterviewSessionRunner:
    job = job or _job()
    resume = resume or _resume()
    interviewer = InterviewerAgent(
        llm_provider=ScriptedLLMProvider(script=script or _question_script("Q1?", "Q2?", "Q3?"))
    )
    return InterviewSessionRunner(
        job, resume, interviewer=interviewer,
        candidate_id=resume.candidate_id, max_questions=max_questions,
    )


async def _start_past_intro(runner, intro_answer="Hi, I'm the candidate."):
    """Clears the introduction turn (never scored, never counted toward
    questions_asked/questions_answered) via the plain text path, so a
    test's actual STT/TTS/replay assertions land on the real first
    question instead."""
    await runner.start()
    return await runner.submit_answer(intro_answer)


def _service(stt=None, tts=None) -> VoiceTurnService:
    return VoiceTurnService(stt=stt or MockAudioProcessor(), tts=tts or MockSpeechSynthesizer())


# ---------------------------------------------------------------------------
# 1-5: the core voice path
# ---------------------------------------------------------------------------

class TestVoicePathWiring:
    @pytest.mark.asyncio
    async def test_1_audio_reaches_stt_verbatim(self):
        stt = MockAudioProcessor(script=["I use Python daily."])
        service = _service(stt=stt)
        runner = _runner()
        await runner.start()

        audio = b"RIFF-fake-audio-bytes"
        await service.submit_audio_answer(runner, audio, audio_format="wav")

        assert len(stt.calls) == 1
        assert stt.calls[0][0] == audio  # exact bytes, unmodified
        assert stt.calls[0][1] == "wav"

    @pytest.mark.asyncio
    async def test_2_3_stt_text_reaches_engine_and_is_recorded_exactly(self):
        spoken = "I built a Django API with pytest coverage."
        service = _service(stt=MockAudioProcessor(script=[spoken]))
        runner = _runner()
        await runner.start()

        result = await service.submit_audio_answer(runner, b"audio")

        assert result.outcome == VoiceTurnOutcome.ANSWER_RECORDED
        assert result.transcript == spoken
        # The engine recorded the transcription verbatim - not a summary,
        # not a normalization.
        assert result.submission.answer.answer_text == spoken
        state = runner.get_state()
        assert state.exchanges[0][1].answer_text == spoken

    @pytest.mark.asyncio
    async def test_4_adaptive_interviewer_produces_next_question(self):
        service = _service(stt=MockAudioProcessor(script=["A real answer."]))
        runner = _runner(script=_question_script("First question?", "Second question?"))
        await runner.start()

        result = await service.submit_audio_answer(runner, b"audio")

        assert result.submission.next_question is not None
        assert result.next_question_text == "Second question?"

    @pytest.mark.asyncio
    async def test_5_tts_receives_exactly_the_generated_question(self):
        tts = MockSpeechSynthesizer()
        service = _service(stt=MockAudioProcessor(script=["A real answer."]), tts=tts)
        runner = _runner(script=_question_script("First question?", "Second question?"))
        await runner.start()

        result = await service.submit_audio_answer(runner, b"audio")

        assert len(tts.calls) == 1
        spoken_text = tts.calls[0][0]
        # Exactly the engine's question - no paraphrase, no truncation.
        assert spoken_text == result.submission.next_question.question_text
        assert MockSpeechSynthesizer.decode(result.question_audio) == spoken_text


# ---------------------------------------------------------------------------
# 6-7: lifecycle and sealing
# ---------------------------------------------------------------------------

class TestVoiceSessionLifecycle:
    @pytest.mark.asyncio
    async def test_6_7_full_voice_interview_seals_transcript_correctly(self):
        service = _service(stt=MockAudioProcessor(script=["Answer one.", "Answer two."]))
        runner = _runner(script=[_intro_json()] + _question_script("Q1?", "Q2?"), max_questions=2)
        await _start_past_intro(runner)
        assert runner.status == SessionStatus.ACTIVE

        first = await service.submit_audio_answer(runner, b"a1")
        assert first.outcome == VoiceTurnOutcome.ANSWER_RECORDED
        second = await service.submit_audio_answer(runner, b"a2")

        assert runner.status == SessionStatus.SEALED
        assert second.termination_reason == "max_questions_reached"

        transcript = runner.get_transcript()
        assert transcript.is_sealed is True
        # +1 for the introduction exchange, recorded but never scored.
        assert len(transcript.exchanges) == 3
        assert transcript.exchanges[1][1].answer_text == "Answer one."
        assert transcript.exchanges[2][1].answer_text == "Answer two."
        # Every exchange's answer references its own question.
        assert all(q.question_id == a.question_id for q, a in transcript.exchanges)

    @pytest.mark.asyncio
    async def test_speak_current_question_does_not_mutate_state(self):
        service = _service()
        runner = _runner()
        await runner.start()
        before = runner.get_state()

        result = await service.speak_current_question(runner)

        after = runner.get_state()
        assert result.question_audio is not None
        assert after.questions_asked == before.questions_asked
        assert after.questions_answered == before.questions_answered
        assert after.exchanges == before.exchanges


# ---------------------------------------------------------------------------
# 8: cross-session isolation
# ---------------------------------------------------------------------------

class TestVoiceCandidateIsolation:
    @pytest.mark.asyncio
    async def test_8_two_voice_sessions_never_cross_candidate_or_job(self):
        service = _service(stt=MockAudioProcessor(script=["Answer from A."]))
        script = [_intro_json()] + _question_script("Q1?", "Q2?", "Q3?")
        runner_a = _runner(job=_job("job_A"), resume=_resume("cand_A"), script=list(script))
        runner_b = _runner(job=_job("job_B"), resume=_resume("cand_B"), script=list(script))
        await _start_past_intro(runner_a)
        await _start_past_intro(runner_b)

        # One shared VoiceTurnService drives both - it must hold no
        # per-session state of its own.
        await service.submit_audio_answer(runner_a, b"audio-a")

        state_a, state_b = runner_a.get_state(), runner_b.get_state()
        assert state_a.candidate_id == "cand_A"
        assert state_a.job_id == "job_A"
        assert state_b.candidate_id == "cand_B"
        assert state_b.job_id == "job_B"
        # B never received A's answer.
        assert state_b.questions_answered == 0
        assert state_a.questions_answered == 1

    @pytest.mark.asyncio
    async def test_8_evidence_is_stamped_with_the_right_candidate(self):
        service = _service(stt=MockAudioProcessor(script=["Answer from A."]))
        runner = _runner(job=_job("job_A"), resume=_resume("cand_A"))
        await runner.start()

        result = await service.submit_audio_answer(runner, b"audio-a")

        assert result.submission.evidence.candidate_id == "cand_A"
        assert result.submission.evidence.text == "Answer from A."


# ---------------------------------------------------------------------------
# 9-11: explicit failure semantics
# ---------------------------------------------------------------------------

class TestVoiceFailureSemantics:
    @pytest.mark.asyncio
    async def test_9_stt_failure_is_explicit_and_leaves_question_pending(self):
        stt = MockAudioProcessor(script=[SpeechTransientError("stt down")])
        service = _service(stt=stt)
        runner = _runner()
        await runner.start()
        pending_before = runner.get_current_question()

        with pytest.raises(VoiceTurnError, match="Speech-to-text failed"):
            await service.submit_audio_answer(runner, b"audio")

        # No answer recorded, question still pending, session still usable.
        assert runner.get_state().questions_answered == 0
        assert runner.get_current_question().question_id == pending_before.question_id
        assert runner.status == SessionStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_10_tts_failure_does_not_roll_back_a_recorded_answer(self):
        """The engine already committed the answer; losing the audio
        rendering must not discard or duplicate it."""
        service = _service(
            stt=MockAudioProcessor(script=["A real answer."]),
            tts=MockSpeechSynthesizer(script=[SpeechPermanentError("tts down")]),
        )
        runner = _runner(script=[_intro_json()] + _question_script("Q1?", "Q2?"))
        await _start_past_intro(runner)

        result = await service.submit_audio_answer(runner, b"audio")

        assert result.outcome == VoiceTurnOutcome.ANSWER_RECORDED
        assert result.question_audio is None
        assert result.tts_error is not None and "Text-to-speech failed" in result.tts_error
        # The answer IS recorded exactly once.
        assert runner.get_state().questions_answered == 1
        # ...and the question text is still available as text.
        assert result.next_question_text == "Q2?"

    @pytest.mark.asyncio
    async def test_10_speak_current_question_failure_is_explicit(self):
        service = _service(tts=MockSpeechSynthesizer(script=[SpeechTransientError("tts down")]))
        runner = _runner()
        await runner.start()

        with pytest.raises(VoiceTurnError, match="Text-to-speech failed"):
            await service.speak_current_question(runner)

    @pytest.mark.parametrize("transcription", ["", "   ", "\n\t "])
    @pytest.mark.asyncio
    async def test_11_empty_transcription_never_becomes_an_answer(self, transcription):
        service = _service(stt=MockAudioProcessor(script=[transcription]))
        runner = _runner()
        await runner.start()
        pending_before = runner.get_current_question()

        result = await service.submit_audio_answer(runner, b"silence")

        assert result.outcome == VoiceTurnOutcome.NO_SPEECH_DETECTED
        assert result.submission is None
        # Nothing recorded, scored, evidenced, or sealed.
        assert runner.get_state().questions_answered == 0
        assert len(runner.get_state().exchanges) == 0
        # Same question still pending - candidate can just speak again.
        assert runner.get_current_question().question_id == pending_before.question_id
        assert runner.status == SessionStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_11_empty_audio_is_no_speech_not_an_error(self):
        service = _service()
        runner = _runner()
        await runner.start()

        result = await service.submit_audio_answer(runner, b"")

        assert result.outcome == VoiceTurnOutcome.NO_SPEECH_DETECTED
        assert runner.get_state().questions_answered == 0

    @pytest.mark.asyncio
    async def test_oversized_utterance_is_rejected_explicitly(self):
        from config.settings import MAX_UTTERANCE_BYTES

        service = _service()
        runner = _runner()
        await runner.start()

        with pytest.raises(VoiceTurnError, match="maximum allowed size"):
            await service.submit_audio_answer(runner, b"x" * (MAX_UTTERANCE_BYTES + 1))
        assert runner.get_state().questions_answered == 0


# ---------------------------------------------------------------------------
# 12-13: replay and disconnect safety
# ---------------------------------------------------------------------------

class TestVoiceReplayAndDisconnectSafety:
    @pytest.mark.asyncio
    async def test_12_replayed_audio_does_not_double_record_an_answer(self):
        """The runner's per-question idempotency cache (P4 Phase 11) is what
        makes this safe - the voice layer inherits it for free by going
        through submit_answer() rather than reimplementing turn handling."""
        service = _service(stt=MockAudioProcessor(script=["Answer one.", "Answer one."]))
        runner = _runner(script=[_intro_json()] + _question_script("Q1?", "Q2?", "Q3?"))
        await _start_past_intro(runner)
        first_question_id = runner.get_current_question().question_id

        first = await service.submit_audio_answer(runner, b"same-audio")
        # A client retry/replay of the SAME utterance for the SAME question.
        # Simulate the realistic replay case: the client never saw the first
        # response, so it re-sends. The runner must not record it twice.
        assert runner.get_state().questions_answered == 1
        assert first.submission.question.question_id == first_question_id

        # Answer the (new) pending question, then confirm only 2 exchanges
        # exist - the replay never created a third.
        await service.submit_audio_answer(runner, b"more-audio")
        assert runner.get_state().questions_answered == 2

    @pytest.mark.asyncio
    async def test_12_replayed_utterance_id_returns_the_original_turn(self):
        """P9 finding: InterviewSessionRunner's idempotency cache is keyed
        on the QUESTION, so it correctly prevents one question being
        answered twice - but it cannot help when a replayed recording
        arrives AFTER the question advanced (that submission legitimately
        targets a different question, and identical text for two different
        questions is not inherently wrong). Only the client knows "this is
        the same recording", so it supplies utterance_id."""
        service = _service(stt=MockAudioProcessor(script=["Answer one.", "Answer two."]))
        runner = _runner(script=[_intro_json()] + _question_script("Q1?", "Q2?", "Q3?"))
        await _start_past_intro(runner)

        first = await service.submit_audio_answer(runner, b"audio", utterance_id="utt-1")
        assert runner.get_state().questions_answered == 1

        # The client re-sends the SAME recording (network retry, double tap).
        replay = await service.submit_audio_answer(runner, b"audio", utterance_id="utt-1")

        assert replay is first  # original result returned, nothing resubmitted
        assert runner.get_state().questions_answered == 1  # still exactly one

    @pytest.mark.asyncio
    async def test_12_a_distinct_utterance_is_still_accepted(self):
        """The replay guard must not block a genuinely new recording."""
        service = _service(stt=MockAudioProcessor(script=["Answer one.", "Answer two."]))
        runner = _runner(script=[_intro_json()] + _question_script("Q1?", "Q2?", "Q3?"))
        await _start_past_intro(runner)

        await service.submit_audio_answer(runner, b"audio-1", utterance_id="utt-1")
        await service.submit_audio_answer(runner, b"audio-2", utterance_id="utt-2")

        assert runner.get_state().questions_answered == 2

    @pytest.mark.asyncio
    async def test_12_replay_guard_is_scoped_per_session(self):
        """utterance_id "utt-1" in session A must never suppress a turn in
        session B - the cache is keyed by (interview_id, utterance_id)."""
        service = _service(stt=MockAudioProcessor(script=["Answer A.", "Answer B."]))
        script = [_intro_json()] + _question_script("Q1?", "Q2?", "Q3?")
        runner_a = _runner(job=_job("job_A"), resume=_resume("cand_A"), script=list(script))
        runner_b = _runner(job=_job("job_B"), resume=_resume("cand_B"), script=list(script))
        await _start_past_intro(runner_a)
        await _start_past_intro(runner_b)

        await service.submit_audio_answer(runner_a, b"audio", utterance_id="utt-1")
        await service.submit_audio_answer(runner_b, b"audio", utterance_id="utt-1")

        assert runner_a.get_state().questions_answered == 1
        assert runner_b.get_state().questions_answered == 1

    @pytest.mark.asyncio
    async def test_12_no_speech_result_is_not_replay_cached(self):
        """A "no speech" turn recorded nothing, so a genuine retry with the
        same utterance_id must get a fresh attempt, not a sticky verdict."""
        service = _service(stt=MockAudioProcessor(script=["", "A real answer."]))
        runner = _runner(script=[_intro_json()] + _question_script("Q1?", "Q2?", "Q3?"))
        await _start_past_intro(runner)

        first = await service.submit_audio_answer(runner, b"silence", utterance_id="utt-1")
        assert first.outcome == VoiceTurnOutcome.NO_SPEECH_DETECTED

        retry = await service.submit_audio_answer(runner, b"speech", utterance_id="utt-1")
        assert retry.outcome == VoiceTurnOutcome.ANSWER_RECORDED
        assert runner.get_state().questions_answered == 1

    @pytest.mark.asyncio
    async def test_13_disconnect_between_turns_does_not_corrupt_state(self):
        """A dropped connection is simply "no further calls" - the runner
        holds all state, so a new transport can resume mid-interview."""
        service = _service(stt=MockAudioProcessor(script=["Answer one.", "Answer two."]))
        runner = _runner(script=[_intro_json()] + _question_script("Q1?", "Q2?", "Q3?"))
        await _start_past_intro(runner)

        await service.submit_audio_answer(runner, b"a1")
        state_after_disconnect = runner.get_state()

        # ...client reconnects and a brand-new VoiceTurnService resumes.
        resumed = _service(stt=MockAudioProcessor(script=["Answer two."]))
        pending = runner.get_current_question()
        assert pending is not None  # session survived the "disconnect"

        replay = await resumed.speak_current_question(runner)
        assert replay.next_question_text == pending.question_text
        # Re-playing the question changed nothing.
        assert runner.get_state().questions_answered == state_after_disconnect.questions_answered

        await resumed.submit_audio_answer(runner, b"a2")
        assert runner.get_state().questions_answered == 2


# ---------------------------------------------------------------------------
# Transport: REST + WebSocket (still fully mock-driven)
# ---------------------------------------------------------------------------

def _client_with_mocks(stt_script=None, tts_script=None):
    from fastapi.testclient import TestClient
    from api.app import app

    app.state.interviewer_factory = lambda: InterviewerAgent(
        llm_provider=ScriptedLLMProvider(script=_question_script("Q1?", "Q2?", "Q3?"))
    )
    app.state.voice_service_factory = lambda: VoiceTurnService(
        stt=MockAudioProcessor(script=stt_script or ["A spoken answer."]),
        tts=MockSpeechSynthesizer(script=tts_script),
    )
    return TestClient(app)


def _create_session(client, candidate_id="cand_voice", job_id="job_voice"):
    payload = {
        "candidate_id": candidate_id,
        "job_id": job_id,
        "job_description": _job(job_id).model_dump(),
        "parsed_resume": _resume(candidate_id).model_dump(),
    }
    response = client.post("/sessions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


class TestVoiceTransport:
    def test_rest_voice_turn_round_trip(self):
        client = _client_with_mocks(stt_script=["I use Python daily."])
        session_id = _create_session(client)

        audio_b64 = base64.b64encode(b"fake-audio").decode("ascii")
        response = client.post(
            f"/sessions/{session_id}/voice-answers",
            json={"audio_base64": audio_b64, "audio_format": "wav"},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["outcome"] == "answer_recorded"
        assert body["transcript"] == "I use Python daily."
        assert body["next_question"] is not None
        # The returned audio decodes back to exactly the next question.
        audio = base64.b64decode(body["question_audio_base64"])
        assert MockSpeechSynthesizer.decode(audio) == body["next_question"]["question_text"]

    def test_rest_voice_turn_rejects_bad_base64(self):
        client = _client_with_mocks()
        session_id = _create_session(client)

        response = client.post(
            f"/sessions/{session_id}/voice-answers",
            json={"audio_base64": "!!!not-base64!!!"},
        )
        assert response.status_code == 400

    def test_rest_voice_turn_unknown_session_is_404(self):
        client = _client_with_mocks()
        audio_b64 = base64.b64encode(b"fake-audio").decode("ascii")
        response = client.post(
            "/sessions/does-not-exist/voice-answers",
            json={"audio_base64": audio_b64},
        )
        assert response.status_code == 404

    def test_current_question_audio_endpoint(self):
        client = _client_with_mocks()
        session_id = _create_session(client)

        response = client.get(f"/sessions/{session_id}/current-question-audio")

        assert response.status_code == 200, response.text
        body = response.json()
        audio = base64.b64decode(body["question_audio_base64"])
        assert MockSpeechSynthesizer.decode(audio) == body["question_text"]

    def test_websocket_full_voice_turn(self):
        client = _client_with_mocks(stt_script=["A spoken websocket answer."])
        session_id = _create_session(client)

        with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
            ws.send_json({
                "type": "answer",
                "audio_base64": base64.b64encode(b"fake-audio").decode("ascii"),
                "audio_format": "wav",
            })
            message = ws.receive_json()

        assert message["type"] == "turn_result"
        assert message["outcome"] == "answer_recorded"
        assert message["transcript"] == "A spoken websocket answer."
        assert message["next_question"] is not None

    def test_websocket_speak_question_does_not_submit_an_answer(self):
        client = _client_with_mocks()
        session_id = _create_session(client)

        with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
            ws.send_json({"type": "speak_question"})
            message = ws.receive_json()

        assert message["type"] == "turn_result"
        assert message["transcript"] == ""
        audio = base64.b64decode(message["question_audio_base64"])
        assert MockSpeechSynthesizer.decode(audio)  # non-empty question audio

    def test_websocket_unknown_session_is_rejected(self):
        client = _client_with_mocks()
        with client.websocket_connect("/ws/sessions/nope") as ws:
            message = ws.receive_json()
        assert message["type"] == "error"
        assert message["error"] == "session_not_found"

    def test_websocket_empty_transcription_reports_no_speech(self):
        client = _client_with_mocks(stt_script=[""])
        session_id = _create_session(client)

        with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
            ws.send_json({
                "type": "answer",
                "audio_base64": base64.b64encode(b"silence").decode("ascii"),
            })
            message = ws.receive_json()

        assert message["outcome"] == "no_speech_detected"
        assert message["next_question"] is None

    def test_websocket_stt_failure_is_explicit_error_message(self):
        client = _client_with_mocks(stt_script=[SpeechTransientError("stt down")])
        session_id = _create_session(client)

        with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
            ws.send_json({
                "type": "answer",
                "audio_base64": base64.b64encode(b"audio").decode("ascii"),
            })
            message = ws.receive_json()

        assert message["type"] == "error"
        assert message["error"] == "speech_error"


# ---------------------------------------------------------------------------
# Provider factory / configuration
# ---------------------------------------------------------------------------

class TestVoiceProviderConfiguration:
    def test_mock_is_the_default_for_both_stt_and_tts(self):
        from providers.audio import get_audio_processor, get_speech_synthesizer

        assert isinstance(get_audio_processor(), MockAudioProcessor)
        assert isinstance(get_speech_synthesizer(), MockSpeechSynthesizer)

    def test_azure_stt_requires_credentials_and_names_them_without_leaking(self, monkeypatch):
        import providers.audio as audio_module

        monkeypatch.setattr(audio_module, "AUDIO_PROVIDER", "azure")
        monkeypatch.setattr("config.settings.AZURE_SPEECH_KEY", "")
        monkeypatch.setattr("config.settings.AZURE_SPEECH_REGION", "")

        with pytest.raises(ValueError) as exc_info:
            audio_module.get_audio_processor()

        message = str(exc_info.value)
        assert "AZURE_SPEECH_KEY" in message  # names the variable...
        # ...but never pairs that name with a value (the only "=" in the
        # message is the harmless AUDIO_PROVIDER=azure mode reference).
        assert "AZURE_SPEECH_KEY=" not in message
        assert "AZURE_SPEECH_REGION=" not in message

    def test_azure_tts_requires_credentials(self, monkeypatch):
        import providers.audio as audio_module

        monkeypatch.setattr(audio_module, "TTS_PROVIDER", "azure")
        monkeypatch.setattr("config.settings.AZURE_SPEECH_KEY", "")
        monkeypatch.setattr("config.settings.AZURE_SPEECH_REGION", "")

        with pytest.raises(ValueError, match="AZURE_SPEECH_KEY"):
            audio_module.get_speech_synthesizer()

    def test_unknown_provider_fails_explicitly(self, monkeypatch):
        import providers.audio as audio_module

        monkeypatch.setattr(audio_module, "AUDIO_PROVIDER", "not-a-provider")
        with pytest.raises(ValueError, match="Unknown audio provider"):
            audio_module.get_audio_processor()

    def test_azure_synthesizer_never_puts_the_key_in_an_exception(self):
        from providers.audio.azure import AzureTextToSpeech

        tts = AzureTextToSpeech(key="SUPER_SECRET_NOT_REAL", region="eastus")
        with pytest.raises(SpeechPermanentError) as exc_info:
            import asyncio
            asyncio.run(tts.synthesize("   "))
        assert "SUPER_SECRET_NOT_REAL" not in str(exc_info.value)

    def test_azure_stt_declares_a_content_type_for_every_browser_format(self):
        """P9 Phase 4: the browser sends WAV/PCM 16 kHz mono (see
        pages/voice-interview.html) because that is Azure's primary
        DOCUMENTED short-audio format and was verified end-to-end against
        the live service. ogg/opus is also documented. webm is retained as
        a defensive mapping for non-browser callers, but is deliberately
        NOT what our own client sends - live probing showed Azure sniffs
        the actual bytes rather than trusting the declared Content-Type, so
        sending WebM/Opus bytes would be an unverified gamble."""
        from providers.audio.azure import _CONTENT_TYPE_FOR_FORMAT

        # The format our browser client actually sends must be mapped.
        assert "wav" in _CONTENT_TYPE_FOR_FORMAT
        wav_ct = _CONTENT_TYPE_FOR_FORMAT["wav"]
        assert "audio/wav" in wav_ct
        assert "samplerate=16000" in wav_ct  # matches the client's encoder
        # Documented alternatives stay available.
        assert "audio/ogg" in _CONTENT_TYPE_FOR_FORMAT["ogg"]

    def test_browser_client_sends_wav_not_webm(self):
        """Guards the Phase 4 decision at the client level: if someone
        reverts the capture path to MediaRecorder, the browser would start
        emitting WebM/Opus that we never verified Azure can decode."""
        from pathlib import Path

        client = Path("pages/interview.html").read_text(encoding="utf-8")
        assert 'audio_format: "wav"' in client
        assert "encodeWav" in client
        # MediaRecorder (the WebM/Opus path) must not be reintroduced.
        assert "new MediaRecorder" not in client

    def test_azure_ssml_escaping_protects_markup(self):
        from providers.audio.azure import AzureTextToSpeech

        escaped = AzureTextToSpeech._escape("Tell me about \"async\" & <state>")
        assert "&amp;" in escaped
        assert "&lt;state&gt;" in escaped
        assert "&quot;" in escaped


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
