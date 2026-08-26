"""
Azure Speech STT/TTS provider (P9) - the first REAL speech implementation.

Chosen over the alternatives for this project because it offers both STT
and TTS behind one credential/region pair (one thing to configure, one
thing to rotate), has a plain REST surface that needs no native binary or
heavyweight SDK, and returns WAV directly for TTS so the browser can play
the bytes without a transcode step.

Deliberately implemented against the REST endpoints using `httpx` (already
a project dependency for FastAPI's TestClient) rather than the
`azure-cognitiveservices-speech` SDK: the SDK is a large native wheel whose
main value is continuous microphone streaming, and this first vertical
slice sends discrete, already-captured utterances. Adding a native
dependency to every install - including CI, which never makes a real
speech call - is not justified yet. If/when true continuous streaming with
partial transcripts is implemented (P9 Phase 8), that is the point to
revisit the SDK, and only this file would change.

Security: the subscription key is read from the environment by
config/settings.py and passed in here. It is sent ONLY in the
Ocp-Apim-Subscription-Key request header, and is never logged, echoed into
an exception message, written to a transcript, or returned to a client -
error handling below deliberately surfaces status codes and provider error
classes, never request headers.

NOTE: this provider has NOT been validated against the live Azure service
in this project yet - no Azure credentials are configured in this
environment (see the P9 report). Its error classification mirrors
providers/llm/groq.py's proven structure, but treat the first real call as
the actual verification step.
"""
from typing import Any, Dict, Optional

from providers.base import (
    AudioProcessor,
    SpeechPermanentError,
    SpeechSynthesizer,
    SpeechTransientError,
)

# Azure Speech REST hosts are region-scoped.
_STT_HOST = "https://{region}.stt.speech.microsoft.com"
_TTS_HOST = "https://{region}.tts.speech.microsoft.com"

_STT_PATH = "/speech/recognition/conversation/cognitiveservices/v1"
_TTS_PATH = "/cognitiveservices/v1"

# Azure returns this RecognitionStatus when the audio contained no
# recognizable speech. That is a legitimate RESULT ("nothing was said"),
# not a failure - utils/voice_turn.py turns an empty string into a
# NO_SPEECH_DETECTED outcome rather than an answer.
_NO_MATCH_STATUSES = {"NoMatch", "InitialSilenceTimeout", "BabbleTimeout"}

_CONTENT_TYPE_FOR_FORMAT = {
    "wav": "audio/wav; codecs=audio/pcm; samplerate=16000",
    "pcm": "audio/wav; codecs=audio/pcm; samplerate=16000",
    "ogg": "audio/ogg; codecs=opus",
    "opus": "audio/ogg; codecs=opus",
    "webm": "audio/webm; codecs=opus",
}

DEFAULT_TIMEOUT_SECONDS = 30.0


def _classify(status_code: int, detail: str) -> Exception:
    """Map an HTTP status to the transient/permanent split the voice layer
    understands. Same rationale as providers/llm/groq.py._classify: retrying
    an auth or malformed-request failure can never help, so it must fail
    fast rather than burn retries."""
    if status_code in (408, 429) or status_code >= 500:
        return SpeechTransientError(f"Azure Speech transient failure (HTTP {status_code}): {detail}")
    return SpeechPermanentError(f"Azure Speech request failed (HTTP {status_code}): {detail}")


class AzureSpeechToText(AudioProcessor):
    """Azure Speech -> text via the REST recognition endpoint."""

    def __init__(
        self,
        key: str,
        region: str,
        language: str = "en-US",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        if not key or not region:
            raise SpeechPermanentError("Azure Speech key and region are required")
        self._key = key
        self.region = region
        self.language = language
        self.timeout = timeout

    async def transcribe(self, audio_data: bytes, format: str = "wav") -> str:
        import httpx

        content_type = _CONTENT_TYPE_FOR_FORMAT.get(
            format.lower(), _CONTENT_TYPE_FOR_FORMAT["wav"]
        )
        url = _STT_HOST.format(region=self.region) + _STT_PATH

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    params={"language": self.language, "format": "simple"},
                    headers={
                        "Ocp-Apim-Subscription-Key": self._key,
                        "Content-Type": content_type,
                        "Accept": "application/json",
                    },
                    content=audio_data,
                )
        except httpx.TimeoutException as exc:
            raise SpeechTransientError(f"Azure Speech request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise SpeechTransientError(f"Azure Speech connection error: {exc}") from exc

        if response.status_code != 200:
            # response.text, not headers - the key must never reach a log
            # or an exception message.
            raise _classify(response.status_code, response.text[:200])

        try:
            payload = response.json()
        except ValueError as exc:
            raise SpeechTransientError(f"Azure Speech returned malformed JSON: {exc}") from exc

        status = payload.get("RecognitionStatus")
        if status in _NO_MATCH_STATUSES:
            return ""  # genuinely no speech - a result, not an error
        if status != "Success":
            raise SpeechTransientError(f"Azure Speech recognition status: {status!r}")

        return payload.get("DisplayText", "") or ""

    async def extract_features(self, audio_data: bytes) -> Dict[str, Any]:
        """Not provided by the recognition endpoint. Raised explicitly
        rather than returning fabricated feature values - no caller in the
        voice path uses this, and inventing numbers here would be exactly
        the kind of plausible-looking fake data this project forbids."""
        raise NotImplementedError(
            "AzureSpeechToText does not provide audio feature extraction"
        )


class AzureTextToSpeech(SpeechSynthesizer):
    """Text -> Azure Speech neural TTS via the REST synthesis endpoint.

    Returns 16 kHz 32 kbps mono WAV (riff-16khz-16bit-mono-pcm), which
    browsers play natively from a Blob without any decoding step.
    """

    OUTPUT_FORMAT = "riff-16khz-16bit-mono-pcm"

    def __init__(
        self,
        key: str,
        region: str,
        voice: str = "en-US-JennyNeural",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        if not key or not region:
            raise SpeechPermanentError("Azure Speech key and region are required")
        self._key = key
        self.region = region
        self.voice = voice
        self.timeout = timeout

    @property
    def audio_format(self) -> str:
        return "wav"

    @staticmethod
    def _escape(text: str) -> str:
        """SSML-escape the question text. The question comes from our own
        adaptive engine rather than from a candidate, but it is still
        interpolated into a markup document, so escaping is required for
        correctness (an apostrophe or ampersand in a question would
        otherwise corrupt the SSML) as well as safety."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )

    async def synthesize(self, text: str, voice: Optional[str] = None) -> bytes:
        import httpx

        if not text or not text.strip():
            raise SpeechPermanentError("Cannot synthesize empty text")

        selected_voice = voice or self.voice
        # The text is passed through verbatim (only SSML-escaped) - never
        # paraphrased or truncated, per SpeechSynthesizer.synthesize's
        # contract: the spoken question and the transcript question must be
        # the same question.
        ssml = (
            f"<speak version='1.0' xml:lang='en-US'>"
            f"<voice xml:lang='en-US' name='{selected_voice}'>"
            f"{self._escape(text)}"
            f"</voice></speak>"
        )
        url = _TTS_HOST.format(region=self.region) + _TTS_PATH

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    headers={
                        "Ocp-Apim-Subscription-Key": self._key,
                        "Content-Type": "application/ssml+xml",
                        "X-Microsoft-OutputFormat": self.OUTPUT_FORMAT,
                        "User-Agent": "openhire-voice",
                    },
                    content=ssml.encode("utf-8"),
                )
        except httpx.TimeoutException as exc:
            raise SpeechTransientError(f"Azure TTS request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise SpeechTransientError(f"Azure TTS connection error: {exc}") from exc

        if response.status_code != 200:
            raise _classify(response.status_code, response.text[:200])

        audio = response.content
        if not audio:
            raise SpeechTransientError("Azure TTS returned empty audio")
        return audio
