"""Configuration loading from environment variables and defaults."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import structlog
from dotenv import load_dotenv


DEFAULT_OPENAI_MODEL = "gpt-realtime-2"
DEFAULT_OPENAI_VOICE = "alloy"
DEFAULT_OPENING_GREETING_INSTRUCTIONS = (
    "Start with a warm, brief greeting in one or two short sentences. "
    "The listener just finished microphone setup — do not ask them to say hello. "
    "Invite them to ask you a question or tell you something."
)
DEFAULT_ASSISTANT_INSTRUCTIONS = (
    "You are a friendly voice assistant for children. "
    "Speak clearly, keep responses brief, and use age-appropriate language."
)


@dataclass
class Config:
    openai_api_key: str = ""
    openai_model: str = DEFAULT_OPENAI_MODEL
    openai_voice: str = DEFAULT_OPENAI_VOICE
    assistant_instructions: str = DEFAULT_ASSISTANT_INSTRUCTIONS
    device_host: str = "0.0.0.0"
    device_port: int = 8765
    log_level: str = "INFO"
    mock_mode: bool = False
    max_mock_iterations: int = 20
    web_enabled: bool = False
    web_port: int = 8080


def load_config() -> Config:
    """Load configuration from .env file and environment variables."""
    load_dotenv()

    return Config(
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
        openai_voice=os.getenv("OPENAI_VOICE", DEFAULT_OPENAI_VOICE),
        assistant_instructions=os.getenv(
            "ASSISTANT_INSTRUCTIONS",
            DEFAULT_ASSISTANT_INSTRUCTIONS,
        ),
        device_host=os.getenv("DEVICE_HOST", "0.0.0.0"),
        device_port=int(os.getenv("DEVICE_PORT", "8765")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        mock_mode=os.getenv("MOCK_MODE", "false").lower() in ("true", "1", "yes"),
        max_mock_iterations=int(os.getenv("MAX_MOCK_ITERATIONS", "20")),
        web_enabled=os.getenv("WEB_ENABLED", "false").lower() in ("true", "1", "yes"),
        web_port=int(os.getenv("WEB_PORT", "8080")),
    )


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog with a human-readable console renderer."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
