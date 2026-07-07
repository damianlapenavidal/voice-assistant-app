"""Tests for protocol message validation."""

import json

import pytest
from pydantic import ValidationError

from voice_assistant.core.message import (
    AudioConfig,
    AudioFramePayload,
    HelloAckPayload,
    HelloPayload,
    Message,
    MessageType,
    SetVolumePayload,
    create_message,
    parse_message,
)


class TestMessageTypeEnum:
    """Every protocol message type must exist in the enum."""

    @pytest.mark.parametrize(
        "name",
        [
            "HELLO",
            "HELLO_ACK",
            "DEVICE_STATUS",
            "START_AUDIO_STREAM",
            "STOP_AUDIO_STREAM",
            "AUDIO_FRAME",
            "PLAY_AUDIO",
            "SET_VOLUME",
            "SHUTDOWN_DEVICE",
            "MUTE_MIC",
            "UNMUTE_MIC",
            "PLAYBACK_COMPLETE",
            "PING",
            "PONG",
            "ERROR",
        ],
    )
    def test_message_type_exists(self, name: str) -> None:
        assert MessageType[name].value == name

    def test_total_message_types(self) -> None:
        assert len(MessageType) == 17


class TestCreateMessage:
    """create_message() should build a Message with auto-timestamp."""

    def test_creates_hello_message(self) -> None:
        msg = create_message(
            MessageType.HELLO,
            {
                "device_id": "test-001",
                "device_type": "pi5",
                "firmware_version": "0.1.0",
                "capabilities": ["audio_capture"],
            },
        )
        assert msg.type == MessageType.HELLO
        assert msg.payload is not None
        assert msg.payload["device_id"] == "test-001"

    def test_creates_message_without_payload(self) -> None:
        msg = create_message(MessageType.START_AUDIO_STREAM)
        assert msg.type == MessageType.START_AUDIO_STREAM
        assert msg.payload is None

    def test_timestamp_auto_generated(self) -> None:
        msg = create_message(MessageType.PING)
        assert msg.timestamp is not None
        assert "T" in msg.timestamp  # ISO 8601

    @pytest.mark.parametrize("msg_type", list(MessageType))
    def test_create_message_for_each_type(self, msg_type: MessageType) -> None:
        msg = create_message(msg_type)
        assert msg.type == msg_type
        assert msg.timestamp is not None


class TestMessageSerialization:
    """Message should round-trip through JSON."""

    def test_serialize_to_json(self) -> None:
        msg = create_message(
            MessageType.AUDIO_FRAME,
            {"audio": "SGVsbG8=", "sequence_number": 1, "timestamp": "2026-06-30T00:00:00Z"},
        )
        raw = msg.model_dump_json()
        data = json.loads(raw)
        assert data["type"] == "AUDIO_FRAME"
        assert data["payload"]["audio"] == "SGVsbG8="

    def test_serialize_no_payload(self) -> None:
        msg = create_message(MessageType.STOP_AUDIO_STREAM)
        raw = msg.model_dump_json()
        data = json.loads(raw)
        assert data["type"] == "STOP_AUDIO_STREAM"
        assert data["payload"] is None


class TestParseMessage:
    """parse_message() should validate and return a Message from JSON."""

    def test_parse_hello(self) -> None:
        raw = json.dumps(
            {
                "type": "HELLO",
                "payload": {
                    "device_id": "d1",
                    "device_type": "pi5",
                    "firmware_version": "1.0",
                    "capabilities": [],
                },
                "timestamp": "2026-06-30T00:00:00Z",
            }
        )
        msg = parse_message(raw)
        assert msg.type == MessageType.HELLO
        assert msg.payload["device_id"] == "d1"

    def test_parse_set_volume(self) -> None:
        raw = json.dumps({"type": "SET_VOLUME", "payload": {"volume": 50}})
        msg = parse_message(raw)
        assert msg.type == MessageType.SET_VOLUME

    def test_parse_ping(self) -> None:
        raw = json.dumps({"type": "PING", "payload": {"timestamp": "2026-06-30T00:00:00Z"}})
        msg = parse_message(raw)
        assert msg.type == MessageType.PING

    def test_parse_invalid_json_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_message("not json at all")

    def test_parse_unknown_type_raises(self) -> None:
        raw = json.dumps({"type": "UNKNOWN_TYPE", "payload": {}})
        with pytest.raises(ValidationError):
            parse_message(raw)

    def test_parse_missing_type_raises(self) -> None:
        raw = json.dumps({"payload": {"foo": "bar"}})
        with pytest.raises(ValidationError):
            parse_message(raw)


class TestPayloadValidation:
    """Payload models enforce constraints."""

    def test_set_volume_valid(self) -> None:
        sv = SetVolumePayload(volume=50)
        assert sv.volume == 50

    def test_set_volume_min(self) -> None:
        sv = SetVolumePayload(volume=0)
        assert sv.volume == 0

    def test_set_volume_max(self) -> None:
        sv = SetVolumePayload(volume=100)
        assert sv.volume == 100

    def test_set_volume_too_high_raises(self) -> None:
        with pytest.raises(ValidationError):
            SetVolumePayload(volume=101)

    def test_set_volume_negative_raises(self) -> None:
        with pytest.raises(ValidationError):
            SetVolumePayload(volume=-1)

    def test_hello_payload_requires_fields(self) -> None:
        with pytest.raises(ValidationError):
            HelloPayload()  # type: ignore[call-arg]

    def test_hello_payload_valid(self) -> None:
        hp = HelloPayload(
            device_id="d1",
            device_type="pi5",
            firmware_version="1.0",
            capabilities=["audio_capture"],
        )
        assert hp.device_type == "pi5"

    def test_hello_payload_invalid_device_type(self) -> None:
        with pytest.raises(ValidationError):
            HelloPayload(
                device_id="d1",
                device_type="unknown",
                firmware_version="1.0",
                capabilities=[],
            )

    def test_audio_config_defaults(self) -> None:
        ac = AudioConfig()
        assert ac.sample_rate == 24000
        assert ac.format == "pcm16"
        assert ac.channels == 1

    def test_hello_ack_payload_defaults(self) -> None:
        ack = HelloAckPayload(session_id="s1")
        assert ack.audio_config.sample_rate == 24000

    def test_audio_frame_payload(self) -> None:
        afp = AudioFramePayload(
            audio="SGVsbG8=",
            sequence_number=1,
            timestamp="2026-06-30T00:00:00Z",
        )
        assert afp.sequence_number == 1
