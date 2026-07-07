"""Tests for mock transport behavior."""

import pytest

from voice_assistant.core.message import MessageType, create_message
from voice_assistant.transport.base import TransportError
from voice_assistant.transport.mock_transport import MockDeviceState, MockTransport


class TestMockTransportConnection:
    """Connection lifecycle tests."""

    async def test_starts_disconnected(self) -> None:
        t = MockTransport()
        assert not t.is_connected
        assert t._state == MockDeviceState.DISCONNECTED

    async def test_connect_makes_connected(self) -> None:
        t = MockTransport()
        await t.connect()
        assert t.is_connected

    async def test_disconnect_after_connect(self) -> None:
        t = MockTransport()
        await t.connect()
        await t.disconnect()
        assert not t.is_connected

    async def test_double_connect_raises(self) -> None:
        t = MockTransport()
        await t.connect()
        with pytest.raises(TransportError, match="Already connected"):
            await t.connect()


class TestMockTransportHelloHandshake:
    """First receive should be HELLO; sending HELLO_ACK transitions state."""

    async def test_first_receive_is_hello(self) -> None:
        t = MockTransport()
        await t.connect()
        msg = await t.receive_message()
        assert msg.type == MessageType.HELLO
        assert msg.payload is not None
        assert msg.payload["device_id"] == "mock-device-001"

    async def test_hello_ack_transitions_state(self) -> None:
        t = MockTransport()
        await t.connect()
        await t.receive_message()  # HELLO
        ack = create_message(
            MessageType.HELLO_ACK,
            {"session_id": "s1", "audio_config": {"sample_rate": 24000, "format": "pcm16", "channels": 1}},
        )
        await t.send_message(ack)
        assert t._state == MockDeviceState.HELLO_SENT


class TestMockTransportStreaming:
    """START/STOP_AUDIO_STREAM control audio frame generation."""

    async def _setup_streaming(self) -> MockTransport:
        t = MockTransport()
        await t.connect()
        await t.receive_message()  # HELLO
        ack = create_message(
            MessageType.HELLO_ACK,
            {"session_id": "s1", "audio_config": {"sample_rate": 24000, "format": "pcm16", "channels": 1}},
        )
        await t.send_message(ack)
        return t

    async def test_start_stream_produces_audio_frames(self) -> None:
        t = await self._setup_streaming()
        start = create_message(MessageType.START_AUDIO_STREAM)
        await t.send_message(start)
        assert t._state == MockDeviceState.STREAMING

        msg = await t.receive_message()
        assert msg.type == MessageType.AUDIO_FRAME
        assert msg.payload is not None
        assert "audio" in msg.payload

    async def test_audio_frames_have_incrementing_sequence(self) -> None:
        t = await self._setup_streaming()
        await t.send_message(create_message(MessageType.START_AUDIO_STREAM))

        msg1 = await t.receive_message()
        msg2 = await t.receive_message()
        assert msg1.payload["sequence_number"] < msg2.payload["sequence_number"]

    async def test_stop_stream_changes_state(self) -> None:
        t = await self._setup_streaming()
        await t.send_message(create_message(MessageType.START_AUDIO_STREAM))
        await t.send_message(create_message(MessageType.STOP_AUDIO_STREAM))
        assert t._state == MockDeviceState.STOPPED

    async def test_stopped_state_returns_status(self) -> None:
        t = await self._setup_streaming()
        await t.send_message(create_message(MessageType.START_AUDIO_STREAM))
        await t.send_message(create_message(MessageType.STOP_AUDIO_STREAM))

        msg = await t.receive_message()
        assert msg.type == MessageType.DEVICE_STATUS


class TestMockTransportDisconnected:
    """Operations on a disconnected transport should raise TransportError."""

    async def test_send_when_disconnected_raises(self) -> None:
        t = MockTransport()
        with pytest.raises(TransportError, match="Not connected"):
            await t.send_message(create_message(MessageType.PING))

    async def test_receive_when_disconnected_raises(self) -> None:
        t = MockTransport()
        with pytest.raises(TransportError, match="Not connected"):
            await t.receive_message()

    async def test_shutdown_device_disconnects(self) -> None:
        t = MockTransport()
        await t.connect()
        await t.send_message(create_message(MessageType.SHUTDOWN_DEVICE))
        assert not t.is_connected
