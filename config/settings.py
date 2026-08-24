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
