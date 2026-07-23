"""Tests for launcher target selection and handshake target verification."""

from __future__ import annotations

import pytest

from voice_assistant.config import Config, expected_device_type, load_config
from voice_assistant.core.message import MessageType, create_message
from voice_assistant.core.session import SessionManager
from voice_assistant.transport.base import TransportError
from voice_assistant.transport.mock_transport import MockTransport


class TestExpectedDeviceType:
    def test_pi5_maps_to_pi5(self) -> None:
        assert expected_device_type("pi5") == "pi5"

    def test_pizero2w_maps_to_pi_zero_2w(self) -> None:
        assert expected_device_type("pizero2w") == "pi_zero_2w"

    def test_is_case_insensitive_and_trims(self) -> None:
        assert expected_device_type("  PiZero2W  ") == "pi_zero_2w"

    def test_empty_target_means_no_constraint(self) -> None:
        assert expected_device_type("") is None

    def test_unknown_target_means_no_constraint(self) -> None:
        assert expected_device_type("pi4") is None


class TestConfigTarget:
    def test_defaults_to_empty(self) -> None:
        assert Config().target == ""

    def test_read_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOICE_ASSISTANT_TARGET", "pizero2w")
        assert load_config().target == "pizero2w"

    def test_absent_environment_leaves_it_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("VOICE_ASSISTANT_TARGET", raising=False)
        assert load_config().target == ""


def _hello(device_type: str):
    return create_message(
        MessageType.HELLO,
        {
            "device_id": "test-device",
            "device_type": device_type,
            "firmware_version": "0.1.0",
            "capabilities": ["audio_capture", "audio_playback"],
        },
    )


class TestHandshakeTargetVerification:
    """The launcher selects a board; the handshake must confirm it is that board.

    Without this, ./scripts/start-pizero2w.sh would happily run a full session
    against the Pi 5 if that is what connected.
    """

    async def test_matching_device_type_completes_handshake(self) -> None:
        transport = MockTransport()
        await transport.connect()
        sm = SessionManager(transport, config=Config(target="pizero2w"))
        await sm._complete_handshake(_hello("pi_zero_2w"))
        assert sm.handshake_complete is True

    async def test_mismatched_device_type_is_rejected(self) -> None:
        sm = SessionManager(MockTransport(), config=Config(target="pizero2w"))
        with pytest.raises(TransportError, match="Target mismatch"):
            await sm._complete_handshake(_hello("pi5"))
        assert sm.handshake_complete is False

    async def test_error_names_both_the_target_and_the_device(self) -> None:
        sm = SessionManager(MockTransport(), config=Config(target="pi5"))
        with pytest.raises(TransportError) as exc:
            await sm._complete_handshake(_hello("pi_zero_2w"))
        message = str(exc.value)
        assert "pi5" in message
        assert "pi_zero_2w" in message

    async def test_no_target_accepts_any_device(self) -> None:
        """Plain `python -m voice_assistant` must not require a target."""
        transport = MockTransport()
        await transport.connect()
        sm = SessionManager(transport, config=Config(target=""))
        await sm._complete_handshake(_hello("pi5"))
        assert sm.handshake_complete is True

    async def test_missing_device_type_is_rejected_when_target_is_set(self) -> None:
        sm = SessionManager(MockTransport(), config=Config(target="pi5"))
        with pytest.raises(TransportError, match="Target mismatch"):
            await sm._complete_handshake(create_message(MessageType.HELLO, {}))
