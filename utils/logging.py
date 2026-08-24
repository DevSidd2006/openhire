"""
Logging utilities for the system.
"""
import logging
import sys
from pathlib import Path
from config.settings import DEBUG, OUTPUTS_DIR

# Create logs directory
LOGS_DIR = OUTPUTS_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)


def setup_logging(name: str = "ai_interview_agents") -> logging.Logger:
    """Set up logging configuration."""
    logger = logging.getLogger(name)

    # Only configure if not already configured
    if logger.handlers:
        return logger

    # Set log level
    level = logging.DEBUG if DEBUG else logging.INFO
    logger.setLevel(level)

    # Console handler
    # Reconfigure stdout to UTF-8 where supported (e.g. Windows' default
    # cp1252 console encoding cannot print characters like the checkmarks
    # this project logs, and raises UnicodeEncodeError on every such line).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File handler
    log_file = LOGS_DIR / f"{name}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s"
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance."""
    return logging.getLogger(name)
