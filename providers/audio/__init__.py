"""
Audio processor factory and implementations.
"""
from config.settings import AUDIO_PROVIDER
from providers.base import AudioProcessor
from providers.audio.mock import MockAudioProcessor


def get_audio_processor() -> AudioProcessor:
    """Factory function to get audio processor based on configuration."""
    if AUDIO_PROVIDER == "mock":
        return MockAudioProcessor()
    else:
        raise ValueError(f"Unknown audio provider: {AUDIO_PROVIDER}")


__all__ = ["get_audio_processor", "AudioProcessor", "MockAudioProcessor"]
