"""
P5: FastAPI application entry point.

Runnable in mock mode with no API key:

    python -m uvicorn api.app:app --reload

Starting the server does not call any LLM or provider - config/settings.py
is only imported for LLM_PROVIDER/OPENAI_API_KEY reporting on the health
endpoint (never required to be set), and providers/llm/mock.py is used by
default (LLM_PROVIDER=mock) exactly as the rest of the project already runs
in mock mode. If LLM_PROVIDER=openai, InterviewerAgent's normal provider
resolution (providers/llm/__init__.get_llm_provider) is used unchanged -
this file never overrides or silently falls back on that choice.
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.errors import register_exception_handlers
from api.models import HealthResponse
from api.registry import SessionRegistry
from api.routes.interview import router as interview_router
from api.routes.voice import router as voice_router
from config.settings import LLM_PROVIDER

app = FastAPI(
    title="OpenHire Live Interview API",
    description=(
        "Thin HTTP transport over the existing adaptive interview engine "
        "(InterviewSessionRunner). Contains no interview intelligence of "
        "its own - see utils/interview_session.py for the source of truth."
    ),
    version="0.1.0",
)

app.state.registry = SessionRegistry()
# Optional test seam: a zero-arg callable returning an InterviewerAgent,
# used in place of InterviewSessionRunner's own default (InterviewerAgent()
# using get_llm_provider()) when set. None (the default, always true for a
# real deployment) means "use the runner's normal default" - this is a pure
# wiring hook, never interview logic, so it stays out of api/routes/interview.py.
app.state.interviewer_factory = None
# P9: the same kind of pure wiring seam as interviewer_factory above - a
# zero-arg callable returning a VoiceTurnService, used in place of the
# provider factories' defaults (providers/audio/__init__.py). None (the
# default, always true for a real deployment) means "resolve STT/TTS from
# AUDIO_PROVIDER/TTS_PROVIDER as normal". Never voice or interview logic.
app.state.voice_service_factory = None

register_exception_handlers(app)
app.include_router(interview_router)
app.include_router(voice_router)


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    """No LLM call, no provider call, no API key required - just confirms
    the process is up (P5 Phase 13)."""
    return HealthResponse(status="ok")


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {"service": "openhire-live-interview-api", "llm_provider": LLM_PROVIDER}


# P9: serve the voice client from the API's OWN origin.
#
# Necessary, not cosmetic: pages/voice-interview.html builds its WebSocket
# URL from `location.host`, so opening it as a file:// URL yields an empty
# host and the socket can never connect. Serving it here also keeps the
# browser's fetch/WebSocket same-origin, so no CORS configuration is
# required for the vertical slice. Read-only static hosting of one
# directory - no upload path, no user-supplied path is ever joined here, so
# it introduces no traversal surface of its own (StarletteStaticFiles
# normalizes and confines paths beneath the mounted directory).
_PAGES_DIR = Path(__file__).resolve().parent.parent / "pages"
if _PAGES_DIR.is_dir():
    app.mount("/app", StaticFiles(directory=str(_PAGES_DIR), html=True), name="pages")
