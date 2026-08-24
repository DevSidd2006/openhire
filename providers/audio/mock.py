"""
Mock audio processor for testing.
"""
from typing import Any, Dict

from providers.base import AudioProcessor


class MockAudioProcessor(AudioProcessor):
    """Mock audio processor for testing."""

    async def transcribe(self, audio_data: bytes, format: str = "wav") -> str:
        """Return mock transcription."""
        return "This is a mock transcription of the audio content."

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
