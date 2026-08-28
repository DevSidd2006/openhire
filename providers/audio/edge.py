"""
Edge TTS (Microsoft Edge Neural Text-to-Speech) provider.

Delivers high-quality neural speech synthesis (e.g. en-IN-NeerjaNeural,
en-IN-PrabhatNeural) with zero API keys or cloud service subscriptions.
"""
from typing import Optional

from providers.base import SpeechPermanentError, SpeechSynthesizer, SpeechTransientError


class EdgeTextToSpeech(SpeechSynthesizer):
    """Text-to-speech implementation backed by edge-tts."""

    def __init__(self, voice: Optional[str] = None, rate: str = "+0%", pitch: str = "+0Hz"):
        self.default_voice = voice or "en-IN-NeerjaNeural"
        self.rate = rate
        self.pitch = pitch

    @property
    def audio_format(self) -> str:
        return "mp3"

    async def synthesize(self, text: str, voice: Optional[str] = None) -> bytes:
        """Synthesize `text` into MP3 audio bytes using Microsoft Edge Neural TTS."""
        if not text or not text.strip():
            raise SpeechPermanentError("Cannot synthesize empty text")

        try:
            import edge_tts
        except ImportError as exc:
            raise SpeechPermanentError(
                "edge-tts is not installed. Install it via `pip install edge-tts`."
            ) from exc

        target_voice = voice or self.default_voice
        try:
            communicate = edge_tts.Communicate(
                text=text.strip(),
                voice=target_voice,
                rate=self.rate,
                pitch=self.pitch,
            )
            audio_chunks = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_chunks.append(chunk["data"])

            audio_bytes = b"".join(audio_chunks)
            if not audio_bytes:
                raise SpeechTransientError("Edge TTS returned an empty audio stream")
            return audio_bytes

        except edge_tts.exceptions.UnknownVoice:
            raise SpeechPermanentError(f"Unknown Edge TTS voice: '{target_voice}'")
        except Exception as exc:
            raise SpeechTransientError(f"Edge TTS synthesis failed: {exc}") from exc
