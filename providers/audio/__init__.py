"""
Audio provider factories (P9).

Two independent factories, mirroring providers/llm/__init__.py's
get_llm_provider(): one for speech-to-text (AudioProcessor) and one for
text-to-speech (SpeechSynthesizer). Both default to a deterministic mock so
the entire voice layer runs offline with no API key, exactly as the agent
layer does with LLM_PROVIDER=mock.

Neither factory ever prints, logs, or returns a credential - only the
provider name and (for Azure) the region/voice, which are not secrets.
"""
from config.settings import AUDIO_PROVIDER, TTS_PROVIDER
from providers.audio.mock import MockAudioProcessor, MockSpeechSynthesizer
from providers.base import AudioProcessor, SpeechSynthesizer


def get_audio_processor() -> AudioProcessor:
    """Factory function to get the speech-to-text processor based on
    configuration (AUDIO_PROVIDER, or its AUDIO_PROCESSOR alias)."""
    if AUDIO_PROVIDER == "mock":
        return MockAudioProcessor()

    if AUDIO_PROVIDER == "azure":
        from config.settings import AZURE_SPEECH_KEY, AZURE_SPEECH_REGION
        from providers.audio.azure import AzureSpeechToText

        if not AZURE_SPEECH_KEY or not AZURE_SPEECH_REGION:
            raise ValueError(
                "AZURE_SPEECH_KEY and AZURE_SPEECH_REGION are required for "
                "AUDIO_PROVIDER=azure. Set them in .env (gitignored) - never "
                "in a tracked file."
            )
        return AzureSpeechToText(key=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION)

    raise ValueError(f"Unknown audio provider: {AUDIO_PROVIDER}")


def get_speech_synthesizer() -> SpeechSynthesizer:
    """Factory function to get the text-to-speech synthesizer based on
    configuration (TTS_PROVIDER)."""
    if TTS_PROVIDER == "mock":
        return MockSpeechSynthesizer()

    if TTS_PROVIDER == "azure":
        from config.settings import (
            AZURE_SPEECH_KEY,
            AZURE_SPEECH_REGION,
            AZURE_SPEECH_VOICE,
        )
        from providers.audio.azure import AzureTextToSpeech

        if not AZURE_SPEECH_KEY or not AZURE_SPEECH_REGION:
            raise ValueError(
                "AZURE_SPEECH_KEY and AZURE_SPEECH_REGION are required for "
                "TTS_PROVIDER=azure. Set them in .env (gitignored) - never "
                "in a tracked file."
            )
        return AzureTextToSpeech(
            key=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION, voice=AZURE_SPEECH_VOICE
        )

    raise ValueError(f"Unknown TTS provider: {TTS_PROVIDER}")


__all__ = [
    "get_audio_processor",
    "get_speech_synthesizer",
    "AudioProcessor",
    "SpeechSynthesizer",
    "MockAudioProcessor",
    "MockSpeechSynthesizer",
]
