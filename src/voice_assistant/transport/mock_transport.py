"""Mock transport that simulates a device for testing without hardware."""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from enum import Enum, auto

import structlog

from voice_assistant.core.message import (
    Message,
    MessageType,
    create_message,
)
from voice_assistant.transport.base import Transport, TransportError

log = structlog.get_logger()


class MockDeviceState(Enum):
    DISCONNECTED = auto()
    CONNECTED = auto()
    HELLO_SENT = auto()
    STREAMING = auto()
    STOPPED = auto()


class MockTransport(Transport):
    """Simulates a device that sends HELLO, DEVICE_STATUS, and AUDIO_FRAME messages."""

    def __init__(self) -> None:
        self._state = MockDeviceState.DISCONNECTED
        self._call_count = 0
        self._sequence_number = 0
        self.sent_messages: list[Message] = []

    async def connect(self) -> None:
        if self._state != MockDeviceState.DISCONNECTED:
            raise TransportError("Already connected")
        self._state = MockDeviceState.CONNECTED
        log.info("mock_transport.connected")

    async def disconnect(self) -> None:
        self._state = MockDeviceState.DISCONNECTED
        log.info("mock_transport.disconnected")

    async def send_message(self, message: Message) -> None:
        if self._state == MockDeviceState.DISCONNECTED:
            raise TransportError("Not connected")

        self.sent_messages.append(message)
        log.debug("mock_transport.send", type=message.type.value)

        match message.type:
            case MessageType.HELLO_ACK:
                self._state = MockDeviceState.HELLO_SENT
            case MessageType.START_AUDIO_STREAM:
                self._state = MockDeviceState.STREAMING
                self._sequence_number = 0
                log.info("mock_transport.streaming_started")
            case MessageType.STOP_AUDIO_STREAM:
                self._state = MockDeviceState.STOPPED
                log.info("mock_transport.streaming_stopped")
            case MessageType.SHUTDOWN_DEVICE:
                log.info("mock_transport.shutdown_requested")
                self._state = MockDeviceState.DISCONNECTED

    async def receive_message(self) -> Message:
        if self._state == MockDeviceState.DISCONNECTED:
            raise TransportError("Not connected")

        self._call_count += 1

        if self._state == MockDeviceState.CONNECTED:
            self._state = MockDeviceState.HELLO_SENT
            return create_message(
                MessageType.HELLO,
                {
                    "device_id": "mock-device-001",
                    "device_type": "pi5",
                    "firmware_version": "0.1.0-mock",
                    "capabilities": ["audio_capture", "audio_playback", "status"],
                },
            )

        if self._state == MockDeviceState.STREAMING:
            if self._call_count % 10 == 0:
                return self._make_status_message()
            return self._make_audio_frame()

        return self._make_status_message()

    @property
    def is_connected(self) -> bool:
        return self._state != MockDeviceState.DISCONNECTED

    # --- helpers ---

    def _make_audio_frame(self) -> Message:
        self._sequence_number += 1
        fake_audio = base64.b64encode(os.urandom(960)).decode()
        return create_message(
            MessageType.AUDIO_FRAME,
            {
                "audio": fake_audio,
                "sequence_number": self._sequence_number,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _make_status_message(self) -> Message:
        return create_message(
            MessageType.DEVICE_STATUS,
            {
                "battery_percent": 85,
                "cpu_temp": 42.5,
                "is_recording": self._state == MockDeviceState.STREAMING,
                "uptime_seconds": float(self._call_count * 2),
            },
        )
