"""Configuration loading from environment variables and defaults."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import structlog
from dotenv import load_dotenv

from voice_assistant.prompts import load_prompt, load_system_instructions


DEFAULT_OPENAI_MODEL = "gpt-realtime-2.1-mini"
DEFAULT_OPENAI_VOICE = "alloy"
DEFAULT_PERSONA_ID = "oso_animals"
DEFAULT_OPENING_GREETING_PROMPT = "opening_greeting"

# Launcher target name -> the device_type a HELLO from that board must carry.
# The scripts/ launchers export VOICE_ASSISTANT_TARGET; the session uses this
# map to reject a handshake from the wrong board instead of silently running
# a Pi 5 session against the Pi Zero (or vice versa).
TARGET_DEVICE_TYPES: dict[str, str] = {
    "pi5": "pi5",
    "pizero2w": "pi_zero_2w",
}


def expected_device_type(target: str) -> str | None:
    """Return the device_type expected for a launcher target, or None if unknown.

    An unknown/empty target means "no target selected" -- the app accepts any
    board, which is what plain `python -m voice_assistant` should do.
    """
    return TARGET_DEVICE_TYPES.get(target.strip().lower()) or None


@dataclass
class Config:
    openai_api_key: str = ""
    openai_model: str = DEFAULT_OPENAI_MODEL
    openai_voice: str = DEFAULT_OPENAI_VOICE
    persona_id: str = DEFAULT_PERSONA_ID
    assistant_instructions: str = field(
        default_factory=lambda: load_system_instructions(DEFAULT_PERSONA_ID)
    )
    opening_greeting_instructions: str = field(
        default_factory=lambda: load_prompt(DEFAULT_OPENING_GREETING_PROMPT)
    )
    device_host: str = "0.0.0.0"
    device_port: int = 8765
    log_level: str = "INFO"
    # Which Raspberry Pi the launcher selected ("pi5", "pizero2w", or "" for
    # none). Set by scripts/start-target.sh; purely informational to the app
    # except that it gates the handshake device_type check.
    target: str = ""
    mock_mode: bool = False
    max_mock_iterations: int = 20
    web_enabled: bool = False
    web_port: int = 8080
    # Kill switch for binary audio framing. Off forces JSON for every device
    # regardless of what it advertises, without a device-side change or a
    # rollback.
    binary_audio_frames: bool = True
    # Phase 6a of the battery plan: let the user interrupt the assistant by
    # opening a brief listening window in the natural pauses of its own speech.
    # Off by default -- it changes response pacing and costs radio time, both of
    # which want judging by ear on real hardware before becoming the default.
    barge_in: bool = False
    # Phase 7 of the battery plan: reject audio that is too quiet to be the
    # child at the device, so parents across the room and background chatter
    # don't get answered. Off by default *on purpose* -- the levels are logged
    # either way, and the plan's own order is to gather real logs before letting
    # anything be dropped, because a rejection that is never transmitted cannot
    # be reviewed.
    voice_level_gate: bool = False


def load_config() -> Config:
    """Load configuration from .env file and environment variables."""
    load_dotenv()

    persona_id = os.getenv("PERSONA", DEFAULT_PERSONA_ID)

    # ASSISTANT_INSTRUCTIONS is an optional inline override: when set it skips
    # file loading entirely (handy for quick experiments). Otherwise the system
    # prompt is composed from base_system.md + the selected persona file.
    override = os.getenv("ASSISTANT_INSTRUCTIONS")
    if override:
        assistant_instructions = override
    else:
        assistant_instructions = load_system_instructions(persona_id)

    opening_greeting_instructions = load_prompt(
        os.getenv("OPENING_GREETING_PROMPT", DEFAULT_OPENING_GREETING_PROMPT)
    )

    return Config(
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
        openai_voice=os.getenv("OPENAI_VOICE", DEFAULT_OPENAI_VOICE),
        persona_id=persona_id,
        assistant_instructions=assistant_instructions,
        opening_greeting_instructions=opening_greeting_instructions,
        device_host=os.getenv("DEVICE_HOST", "0.0.0.0"),
        device_port=int(os.getenv("DEVICE_PORT", "8765")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        target=os.getenv("VOICE_ASSISTANT_TARGET", ""),
        mock_mode=os.getenv("MOCK_MODE", "false").lower() in ("true", "1", "yes"),
        max_mock_iterations=int(os.getenv("MAX_MOCK_ITERATIONS", "20")),
        web_enabled=os.getenv("WEB_ENABLED", "false").lower() in ("true", "1", "yes"),
        web_port=int(os.getenv("WEB_PORT", "8080")),
        binary_audio_frames=os.getenv("BINARY_AUDIO_FRAMES", "true").lower()
        in ("true", "1", "yes"),
        barge_in=os.getenv("BARGE_IN", "false").lower() in ("true", "1", "yes"),
        voice_level_gate=os.getenv("VOICE_LEVEL_GATE", "false").lower()
        in ("true", "1", "yes"),
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
