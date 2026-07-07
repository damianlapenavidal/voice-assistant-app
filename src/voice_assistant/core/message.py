"""Protocol message types using Pydantic models."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    HELLO = "HELLO"
    HELLO_ACK = "HELLO_ACK"
    DEVICE_STATUS = "DEVICE_STATUS"
    START_AUDIO_STREAM = "START_AUDIO_STREAM"
    STOP_AUDIO_STREAM = "STOP_AUDIO_STREAM"
    AUDIO_FRAME = "AUDIO_FRAME"
    PLAY_AUDIO = "PLAY_AUDIO"
    SET_VOLUME = "SET_VOLUME"
    SHUTDOWN_DEVICE = "SHUTDOWN_DEVICE"
    MUTE_MIC = "MUTE_MIC"
    UNMUTE_MIC = "UNMUTE_MIC"
    PLAYBACK_COMPLETE = "PLAYBACK_COMPLETE"
    CALIBRATION_STATUS = "CALIBRATION_STATUS"
    CALIBRATION_COMPLETE = "CALIBRATION_COMPLETE"
    PING = "PING"
    PONG = "PONG"
    ERROR = "ERROR"


# --- Payload models ---


class HelloPayload(BaseModel):
    device_id: str
    device_type: Literal["pi5", "piZero2W"]
    firmware_version: str
    capabilities: list[str]


class AudioConfig(BaseModel):
    sample_rate: int = 24000
    format: str = "pcm16"
    channels: int = 1


class HelloAckPayload(BaseModel):
    session_id: str
    audio_config: AudioConfig = Field(default_factory=AudioConfig)


class DeviceStatusPayload(BaseModel):
    battery_percent: int | None = None
    cpu_temp: float | None = None
    is_recording: bool
    uptime_seconds: float


class AudioFramePayload(BaseModel):
    audio: str
    sequence_number: int
    timestamp: str


class PlayAudioPayload(BaseModel):
    audio: str
    sequence_number: int = 0
    is_final: bool = False
    duration_ms: int | None = None


class PlaybackCompletePayload(BaseModel):
    sequence_number: int
    duration_ms: int


class SetVolumePayload(BaseModel):
    volume: int = Field(ge=0, le=100)


class ErrorPayload(BaseModel):
    code: str
    message: str
    recoverable: bool


class PingPongPayload(BaseModel):
    timestamp: str


# --- Top-level Message ---


class Message(BaseModel):
    type: MessageType
    payload: dict | None = None
    timestamp: str | None = None


def create_message(
    msg_type: MessageType,
    payload: dict | None = None,
) -> Message:
    """Create a Message with an auto-generated ISO 8601 timestamp."""
    return Message(
        type=msg_type,
        payload=payload,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def parse_message(raw: str) -> Message:
    """Parse a JSON string into a validated Message."""
    data = json.loads(raw)
    return Message.model_validate(data)
