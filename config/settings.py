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

# Embedding Configuration
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "local").lower()

# Vector Store Configuration
VECTOR_STORE_TYPE = os.getenv("VECTOR_STORE_TYPE", "faiss").lower()
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
ASYNC_EXECUTION = True

# Scoring Configuration
CONFIDENCE_THRESHOLD = 0.7

# Audio Configuration
AUDIO_PROVIDER = os.getenv("AUDIO_PROVIDER", "mock").lower()

# Debug Mode
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
