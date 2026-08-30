"""
Configuration settings for the AI Interview Agents system.
"""
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Project paths
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
PROMPTS_DIR = PROJECT_ROOT / "prompts"

# Create output directory if it doesn't exist
OUTPUTS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# LLM Configuration
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "mock").lower()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4-turbo")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# NVIDIA NIM (Inference Microservices): PRIMARY LLM provider with OpenAI-compatible API.
# Supports Nemotron ultra-powerful model with extended thinking. Cloud-hosted at
# integrate.api.nvidia.com. The key is only ever read from the environment - never
# hard-coded, logged, or reported.
NVIDIA_NIM_API_KEY = os.getenv("NVIDIA_NIM_API_KEY", "")
NVIDIA_NIM_MODEL = os.getenv("NVIDIA_NIM_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
NVIDIA_NIM_BASE_URL = os.getenv("NVIDIA_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")

# Groq: Alternative LLM provider (openai/gpt-oss-20b model). Supported but no longer
# the primary target. Kept for backward compatibility.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

# Embedding Configuration
# Production: use API-based (nvidia-nim) to avoid large local models
# Uses nvidia/nemotron-3-embed-1b via free hosted API at integrate.api.nvidia.com
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nvidia/nemotron-3-embed-1b")
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "nvidia-nim").lower()

# Vector Store Configuration
# Production: use mock (in-memory) for simplicity on Render
# Development: can use faiss if needed
VECTOR_STORE_TYPE = os.getenv("VECTOR_STORE_TYPE", "mock").lower()
VECTOR_STORE_PATH = os.getenv("VECTOR_STORE_PATH", str(PROJECT_ROOT / "faiss_index"))

# Interview Configuration
DEFAULT_INTERVIEW_DURATION_SECONDS = 900  # 15 minutes
QUESTION_TYPES = ["technical", "behavioral", "role_specific", "follow_up"]

# Adaptive Interview Configuration (P3). No equivalent constants existed
# before P3 - the pre-P3 interviewer generated a fixed batch of questions
# (question_count passed explicitly by the caller) and never needed a
# question budget or a per-competency follow-up limit of its own.
MAX_QUESTIONS_PER_INTERVIEW = 12  # deterministic upper bound so an adaptive
# interview always terminates even if every competency stays under-evidenced.
MAX_FOLLOW_UPS_PER_COMPETENCY = 2  # caps how many follow-up/probe/clarify
# questions the engine will spend on one competency before moving on or
# accepting the evidence gap - prevents endlessly drilling one topic (P3
# Phase 8).
MIN_CONFIDENCE_FOR_COVERAGE = 0.65  # a competency is only treated as
# "sufficiently covered" for termination purposes once its running
# confidence reaches this threshold AND its evidence_status is "supported" -
# matches CONFIDENCE_THRESHOLD's spirit (0.7) but scoped to per-competency
# interview coverage rather than final scoring confidence.

# Evaluation Configuration
MAX_RETRIES = 3
TIMEOUT_SECONDS = 30

# P8B.4 timeout audit: resume_parser's structured schema (ResumeParseResult
# in schemas/llm_outputs.py) is the largest of any agent's - nested lists of
# education/work_experience/projects/certifications entries, each its own
# sub-schema - so under Groq's strict-mode constrained decoding
# (providers/llm/groq.py._normalize_for_strict) it is also the slowest to
# generate. A real Groq call was observed to exceed the global 30s
# TIMEOUT_SECONDS on its first attempt and succeed in ~1.5s on the retry
# (see the P8B.4 report) - correct, but a wasted 30s wait plus one extra API
# call per occurrence, which matters when the daily quota is the scarce
# resource. Every other agent's real-Groq latency stayed well under 30s
# (jd_analyzer, technical/behavioral evaluator: 1-5s), so this override is
# scoped to resume_parser only - the shared TIMEOUT_SECONDS default is
# unchanged for every other agent.
RESUME_PARSER_TIMEOUT_SECONDS = int(os.getenv("RESUME_PARSER_TIMEOUT_SECONDS", "45"))
ASYNC_EXECUTION = True

# Scoring Configuration
CONFIDENCE_THRESHOLD = 0.7

# Audio Configuration
# P9 fix: the .env in this project defines AUDIO_PROCESSOR, but this
# setting only ever read AUDIO_PROVIDER - so an operator setting
# AUDIO_PROCESSOR=... was silently ignored and the system stayed on mock
# with no warning. Both spellings are now accepted (AUDIO_PROVIDER wins if
# both are set, preserving the documented name); AUDIO_PROCESSOR is
# supported as the alias the existing .env already uses.
AUDIO_PROVIDER = os.getenv("AUDIO_PROVIDER", os.getenv("AUDIO_PROCESSOR", "mock")).lower()

# P9: text-to-speech provider, selected independently of STT - a deployment
# may reasonably transcribe with one service and synthesize with another.
# Defaults to mock so the whole voice layer runs offline, with no API key,
# exactly like LLM_PROVIDER=mock does for the agent layer.
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "mock").lower()

# Azure Speech (P9): STT/TTS implementation. Credentials are
# read ONLY from the environment - never hard-coded, logged, echoed into a
# transcript, or returned through the API.
AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "")
AZURE_SPEECH_VOICE = os.getenv("AZURE_SPEECH_VOICE", "en-US-JennyNeural")

# Edge TTS Configuration (Free neural TTS via Microsoft Edge service)
EDGE_TTS_VOICE = os.getenv("EDGE_TTS_VOICE", "en-IN-NeerjaNeural")
EDGE_TTS_RATE = os.getenv("EDGE_TTS_RATE", "+0%")
EDGE_TTS_PITCH = os.getenv("EDGE_TTS_PITCH", "+0Hz")

# P9 voice-turn safety limits.
MAX_UTTERANCE_BYTES = int(os.getenv("MAX_UTTERANCE_BYTES", str(10 * 1024 * 1024)))  # 10 MB
# Below this many characters, a transcription is treated as "no speech
# detected" rather than a real answer - protects against a stray cough or a
# dropped connection silently becoming a scored interview answer (P9).
MIN_TRANSCRIPT_CHARS = int(os.getenv("MIN_TRANSCRIPT_CHARS", "2"))

# Debug Mode
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
