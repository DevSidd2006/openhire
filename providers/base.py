"""
Base provider interfaces.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class LLMTransientError(Exception):
    """An LLM call failed in a way that is safe/reasonable to retry - rate
    limits, timeouts, connection errors, transient 5xx responses. Providers
    should raise this (not a bare Exception) for failures BaseAgent's retry
    wrapper should retry."""


class LLMPermanentError(Exception):
    """An LLM call failed in a way that will not be fixed by retrying -
    invalid API key, malformed request, unsupported model, etc. Providers
    should raise this so BaseAgent's retry wrapper fails fast instead of
    burning retries on a call that can never succeed."""


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> str:
        """Generate text from a prompt."""
        pass

    @abstractmethod
    async def generate_structured(
        self, prompt: str, schema: Dict[str, Any], **kwargs
    ) -> Dict[str, Any]:
        """Generate structured output matching a schema."""
        pass


class EmbeddingProvider(ABC):
    """Abstract base class for embedding providers."""

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Generate embedding for text."""
        pass

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        pass

    @abstractmethod
    def get_embedding_dimension(self) -> int:
        """Get the dimension of embeddings."""
        pass


class VectorStore(ABC):
    """Abstract base class for vector stores."""

    @abstractmethod
    async def add(self, ids: list[str], texts: list[str], embeddings: list[list[float]]) -> None:
        """Add items to the store."""
        pass

    @abstractmethod
    async def search(self, query_embedding: list[float], top_k: int = 5) -> list[Dict[str, Any]]:
        """Search for similar items."""
        pass

    @abstractmethod
    async def delete(self, ids: list[str]) -> None:
        """Delete items from the store."""
        pass

    @abstractmethod
    async def get(self, ids: list[str]) -> list[Dict[str, Any]]:
        """Retrieve items by ID."""
        pass

    @abstractmethod
    async def clear(self) -> None:
        """Clear the entire store."""
        pass


class SpeechError(Exception):
    """Base class for speech (STT/TTS) provider failures (P9).

    Deliberately mirrors the LLMTransientError/LLMPermanentError split
    above rather than inventing a different failure vocabulary: the voice
    layer must fail EXPLICITLY (P9), never silently turn a failed
    transcription into an empty answer or a failed synthesis into silence.
    """


class SpeechTransientError(SpeechError):
    """A speech call failed in a way that may succeed on retry (network,
    rate limit, 5xx, timeout)."""


class SpeechPermanentError(SpeechError):
    """A speech call failed in a way retrying cannot fix (bad credentials,
    unsupported audio format, malformed request)."""


class AudioProcessor(ABC):
    """Abstract base class for audio processing.

    P9 note: this is OpenHire's SPEECH-TO-TEXT interface and the voice
    layer reuses it as-is rather than introducing a parallel STT
    abstraction - `transcribe()` is exactly the operation the voice turn
    needs. Text-to-speech has no counterpart here (this class predates the
    voice layer and is input-only), so it is added as a separate
    `SpeechSynthesizer` ABC below rather than bolted onto this one: a
    transcriber and a synthesizer are independently swappable, and an
    implementation of one should never be forced to stub the other.
    """

    @abstractmethod
    async def transcribe(self, audio_data: bytes, format: str = "wav") -> str:
        """Transcribe audio to text.

        Implementations MUST raise SpeechTransientError/SpeechPermanentError
        on failure rather than returning "" - an empty string is a valid
        RESULT meaning "no speech detected", which the voice layer treats
        very differently from a failed call (see utils/voice_turn.py).
        """
        pass

    @abstractmethod
    async def extract_features(self, audio_data: bytes) -> Dict[str, Any]:
        """Extract audio features."""
        pass


class SpeechSynthesizer(ABC):
    """Abstract base class for text-to-speech (P9).

    Separate from AudioProcessor by design - see that class's P9 note.
    Kept deliberately minimal: the voice layer only ever needs "turn this
    exact question text into playable audio", and any prosody/voice/rate
    configuration belongs in the concrete implementation's constructor
    (driven by .env), never in this interface.
    """

    @abstractmethod
    async def synthesize(self, text: str, voice: Optional[str] = None) -> bytes:
        """Synthesize `text` into audio bytes.

        `text` is the interviewer's question EXACTLY as the adaptive engine
        produced it - an implementation must never paraphrase, truncate, or
        embellish it (the spoken question and the transcript question must
        be the same question).

        Implementations MUST raise SpeechTransientError/SpeechPermanentError
        on failure rather than returning b"" - silent audio would look to a
        candidate like the interviewer simply said nothing.
        """
        pass

    @property
    def audio_format(self) -> str:
        """Container/encoding of the bytes `synthesize` returns (e.g.
        "wav", "mp3"). Used by the transport to tell the browser how to
        play it; defaults to wav."""
        return "wav"
