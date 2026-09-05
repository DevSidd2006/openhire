"""
Mock audio providers for testing (P9).

Deterministic, offline stand-ins for a real STT/TTS service - the exact
same role providers/llm/mock.py and tests/fakes.py play for the LLM layer.
Every automated voice test in this project runs against these; no test
should ever require a real speech API key or network access.
"""
from typing import Any, Dict, List, Optional

from providers.base import AudioProcessor, SpeechSynthesizer


class MockAudioProcessor(AudioProcessor):
    """Mock speech-to-text.

    Default behavior is unchanged from the pre-P9 version (a fixed
    transcription string), so any existing caller keeps working. P9 adds
    two optional, test-only seams:

      * `script` - successive transcribe() calls return successive entries,
        so a test can simulate a multi-turn interview where each spoken
        answer differs. An entry that is an Exception instance is RAISED
        instead of returned (simulating a provider failure); once the
        script is exhausted the last entry repeats, matching
        tests/fakes.ScriptedLLMProvider's established convention.
      * `transcript_for` - a dict mapping raw audio bytes to the text they
        should transcribe to, for tests that care about the audio->text
        mapping specifically rather than call order.

    Neither seam is used in production: the default constructor
    (`MockAudioProcessor()`) behaves exactly as it always did.
    """

    DEFAULT_TRANSCRIPTION = "This is a mock transcription of the audio content."

    def __init__(
        self,
        script: Optional[List[Any]] = None,
        transcript_for: Optional[Dict[bytes, str]] = None,
    ):
        self.script = list(script) if script else None
        self.transcript_for = dict(transcript_for) if transcript_for else None
        # Every (audio_data, format) pair this processor was asked to
        # transcribe, so a test can assert the audio actually reached STT.
        self.calls: List[tuple] = []
        self._index = 0

    async def transcribe(self, audio_data: bytes, format: str = "wav") -> str:
        """Return mock transcription."""
        self.calls.append((audio_data, format))

        if self.transcript_for is not None and audio_data in self.transcript_for:
            return self.transcript_for[audio_data]

        if self.script:
            if self._index < len(self.script):
                item = self.script[self._index]
                self._index += 1
            else:
                item = self.script[-1]
            if isinstance(item, Exception):
                raise item
            return item

        return self.DEFAULT_TRANSCRIPTION

    async def extract_features(self, audio_data: bytes) -> Dict[str, Any]:
        """Extract mock audio features."""
        return {
            "duration_seconds": 120.0,
            "sample_rate": 16000,
            "channels": 1,
            "energy": 0.5,
            "pitch_avg": 120.0,
            "speech_rate": 150,  # words per minute
        }


class MockSpeechSynthesizer(SpeechSynthesizer):
    """Mock text-to-speech (P9).

    Produces deterministic, inspectable fake audio: the returned bytes are
    simply the UTF-8 encoding of the text prefixed with a marker, so a test
    can assert that the audio handed to the candidate corresponds to
    EXACTLY the question the adaptive engine generated (no paraphrase, no
    truncation) without needing a real decoder.

    `script` follows the same convention as MockAudioProcessor: an entry
    that is an Exception instance is raised, simulating a TTS failure.
    """

    PREFIX = b"MOCK_TTS:"

    def __init__(self, script: Optional[List[Any]] = None):
        self.script = list(script) if script else None
        # Every (text, voice) pair this synthesizer was asked to speak.
        self.calls: List[tuple] = []
        self._index = 0

    async def synthesize(self, text: str, voice: Optional[str] = None) -> bytes:
        self.calls.append((text, voice))

        if self.script:
            if self._index < len(self.script):
                item = self.script[self._index]
                self._index += 1
            else:
                item = self.script[-1]
            if isinstance(item, Exception):
                raise item
            if isinstance(item, bytes):
                return item

        return self.PREFIX + text.encode("utf-8")

    @classmethod
    def decode(cls, audio: bytes) -> str:
        """Inverse of the default synthesize() encoding - lets a test read
        back exactly what text was spoken."""
        if not audio.startswith(cls.PREFIX):
            raise ValueError("Not mock-synthesized audio")
        return audio[len(cls.PREFIX):].decode("utf-8")
