"""Tests for WebSocket transport: server start, connect, send/receive, disconnect."""

import asyncio

import pytest
import websockets

from voice_assistant.core.message import MessageType, create_message, parse_message
from voice_assistant.transport.base import TransportError
from voice_assistant.audio.utils import base64_to_pcm16
from voice_assistant.transport.websocket_transport import (
    _AUDIO_FRAME_STRUCT,
    _PLAY_AUDIO_STRUCT,
    AUDIO_FRAME_TAG,
    HEADER_VERSION,
    PLAY_AUDIO_TAG,
    WebSocketTransport,
)

WS_HOST = "127.0.0.1"
WS_PORT = 9876

SERVER_STARTUP_DELAY = 0.15


async def _start_and_connect(port: int):
    """Start a transport server and connect a test client to it."""
    transport = WebSocketTransport(WS_HOST, port)
    connect_task = asyncio.create_task(transport.connect())
    await asyncio.sleep(SERVER_STARTUP_DELAY)
    client = await websockets.connect(f"ws://{WS_HOST}:{port}")
    await asyncio.wait_for(connect_task, timeout=2)
    return transport, client


class TestWebSocketServerLifecycle:
    """Server starts, accepts a connection, then shuts down."""

    async def test_connect_starts_server_and_waits(self) -> None:
        transport = WebSocketTransport(WS_HOST, WS_PORT)
        connect_task = asyncio.create_task(transport.connect())

        await asyncio.sleep(SERVER_STARTUP_DELAY)
        assert not transport.is_connected

        async with websockets.connect(f"ws://{WS_HOST}:{WS_PORT}"):
            await asyncio.wait_for(connect_task, timeout=2)
            assert transport.is_connected

        await transport.disconnect()
        assert not transport.is_connected

    async def test_disconnect_stops_server(self) -> None:
        transport, client = await _start_and_connect(WS_PORT + 1)
        await client.close()
        await transport.disconnect()
        assert transport._server is None

    async def test_double_connect_raises(self) -> None:
        transport, client = await _start_and_connect(WS_PORT + 2)
        try:
            with pytest.raises(TransportError, match="already running"):
                await transport.connect()
        finally:
            await client.close()
            await transport.disconnect()


class TestWebSocketMessageRoundtrip:
    """Send and receive messages through the WebSocket."""

    async def test_send_message_to_client(self) -> None:
        transport, client = await _start_and_connect(WS_PORT + 10)
        try:
            msg = create_message(MessageType.START_AUDIO_STREAM)
            await transport.send_message(msg)

            raw = await asyncio.wait_for(client.recv(), timeout=2)
            parsed = parse_message(raw)
            assert parsed.type == MessageType.START_AUDIO_STREAM
        finally:
            await client.close()
            await transport.disconnect()

    async def test_receive_message_from_client(self) -> None:
        transport, client = await _start_and_connect(WS_PORT + 11)
        try:
            hello = create_message(
                MessageType.HELLO,
                {
                    "device_id": "test-pi",
                    "device_type": "pi5",
                    "firmware_version": "0.1.0",
                    "capabilities": ["audio_capture"],
                },
            )
            await client.send(hello.model_dump_json())

            received = await asyncio.wait_for(transport.receive_message(), timeout=2)
            assert received.type == MessageType.HELLO
            assert received.payload["device_id"] == "test-pi"
        finally:
            await client.close()
            await transport.disconnect()

    async def test_roundtrip_message(self) -> None:
        """Client sends HELLO, server sends HELLO_ACK back."""
        transport, client = await _start_and_connect(WS_PORT + 12)
        try:
            hello = create_message(
                MessageType.HELLO,
                {
                    "device_id": "roundtrip-pi",
                    "device_type": "pi5",
                    "firmware_version": "1.0.0",
                    "capabilities": ["audio_capture", "audio_playback"],
                },
            )
            await client.send(hello.model_dump_json())

            received_hello = await asyncio.wait_for(transport.receive_message(), timeout=2)
            assert received_hello.type == MessageType.HELLO

            ack = create_message(
                MessageType.HELLO_ACK,
                {
                    "session_id": "test-session-42",
                    "audio_config": {"sample_rate": 24000, "format": "pcm16", "channels": 1},
                },
            )
            await transport.send_message(ack)

            raw_ack = await asyncio.wait_for(client.recv(), timeout=2)
            parsed_ack = parse_message(raw_ack)
            assert parsed_ack.type == MessageType.HELLO_ACK
            assert parsed_ack.payload["session_id"] == "test-session-42"
        finally:
            await client.close()
            await transport.disconnect()


class TestWebSocketDisconnectCleanup:
    """Disconnect and error handling."""

    async def test_send_without_client_raises(self) -> None:
        transport = WebSocketTransport(WS_HOST, WS_PORT + 20)
        with pytest.raises(TransportError, match="No device connected"):
            await transport.send_message(create_message(MessageType.PING))

    async def test_receive_without_client_raises(self) -> None:
        transport = WebSocketTransport(WS_HOST, WS_PORT + 21)
        with pytest.raises(TransportError, match="No device connected"):
            await transport.receive_message()

    async def test_client_disconnect_updates_state(self) -> None:
        transport, client = await _start_and_connect(WS_PORT + 22)
        assert transport.is_connected

        await client.close()
        await asyncio.sleep(0.2)
        assert not transport.is_connected

        await transport.disconnect()

    async def test_disconnect_idempotent(self) -> None:
        transport = WebSocketTransport(WS_HOST, WS_PORT + 23)
        await transport.disconnect()
        await transport.disconnect()


class TestBinaryAudioFraming:
    """Binary AUDIO_FRAME / PLAY_AUDIO framing, once negotiated."""

    async def test_play_audio_is_sent_as_binary_when_enabled(self) -> None:
        transport, client = await _start_and_connect(WS_PORT + 30)
        try:
            transport.set_binary_audio_enabled(True)
            pcm = b"\x11\x22\x33\x44" * 4
            await transport.send_message(create_message(
                MessageType.PLAY_AUDIO,
                {
                    "audio": pcm,
                    "sequence_number": 9,
                    "is_final": True,
                    "duration_ms": 250,
                },
            ))

            raw = await asyncio.wait_for(client.recv(), timeout=2)
            assert isinstance(raw, bytes)

            tag, version, seq, flags, duration, reserved = _PLAY_AUDIO_STRUCT.unpack_from(raw, 0)
            assert tag == PLAY_AUDIO_TAG
            assert version == HEADER_VERSION
            assert seq == 9
            assert flags & 0x01  # is_final
            assert duration == 250
            assert reserved == 0
            assert raw[_PLAY_AUDIO_STRUCT.size:] == pcm
        finally:
            await client.close()
            await transport.disconnect()

    async def test_play_audio_stays_json_when_not_negotiated(self) -> None:
        """The Pi 5 path: raw PCM handed to the transport still arrives as
        base64 JSON, not a mangled string."""
        transport, client = await _start_and_connect(WS_PORT + 31)
        try:
            pcm = b"\x01\x02\x03\x04"
            await transport.send_message(create_message(
                MessageType.PLAY_AUDIO,
                {"audio": pcm, "sequence_number": 1},
            ))

            raw = await asyncio.wait_for(client.recv(), timeout=2)
            assert isinstance(raw, str)
            msg = parse_message(raw)
            assert msg.type == MessageType.PLAY_AUDIO
            assert base64_to_pcm16(msg.payload["audio"]) == pcm
        finally:
            await client.close()
            await transport.disconnect()

    async def test_binary_audio_frame_is_received_and_decoded(self) -> None:
        transport, client = await _start_and_connect(WS_PORT + 32)
        try:
            pcm = b"\xAA\xBB\xCC\xDD"
            capture_ms = 1785000000000
            header = _AUDIO_FRAME_STRUCT.pack(
                AUDIO_FRAME_TAG, HEADER_VERSION, 77, capture_ms, 0,
            )
            await client.send(header + pcm)

            msg = await asyncio.wait_for(transport.receive_message(), timeout=2)
            assert msg.type == MessageType.AUDIO_FRAME
            assert msg.payload["audio"] == pcm
            assert msg.payload["sequence_number"] == 77
            assert "T" in msg.payload["timestamp"]
        finally:
            await client.close()
            await transport.disconnect()

    @pytest.mark.parametrize(
        "port_offset,frame",
        [
            (40, b"\x01"),
            (41, _AUDIO_FRAME_STRUCT.pack(0x99, HEADER_VERSION, 1, 0, 0)),
            (42, _AUDIO_FRAME_STRUCT.pack(AUDIO_FRAME_TAG, 99, 1, 0, 0)),
            (43, _AUDIO_FRAME_STRUCT.pack(AUDIO_FRAME_TAG, HEADER_VERSION, 1, 0, 0)[:9]),
        ],
        ids=["too_short", "wrong_tag", "wrong_version", "truncated_header"],
    )
    async def test_malformed_binary_frame_is_recoverable_error(
        self, port_offset: int, frame: bytes,
    ) -> None:
        """A bad frame must not read as connection loss -- one corrupt audio
        chunk should never tear down a live session."""
        transport, client = await _start_and_connect(WS_PORT + port_offset)
        try:
            await client.send(frame)
            msg = await asyncio.wait_for(transport.receive_message(), timeout=2)

            assert msg.type == MessageType.ERROR
            assert msg.payload["code"] == "MALFORMED_AUDIO_FRAME"
            assert msg.payload["recoverable"] is True
            assert transport.is_connected
        finally:
            await client.close()
            await transport.disconnect()


class TestBinaryRoundTrip:
    """Both directions over one real connection, as the device would drive it."""

    async def test_audio_frame_and_play_audio_round_trip(self) -> None:
        transport, client = await _start_and_connect(WS_PORT + 50)
        try:
            transport.set_binary_audio_enabled(True)

            # Device -> app: binary AUDIO_FRAME, at the real 4800-byte chunk size.
            pcm_up = bytes(range(256)) * 18 + bytes(range(192))
            assert len(pcm_up) == 4800
            await client.send(_AUDIO_FRAME_STRUCT.pack(
                AUDIO_FRAME_TAG, HEADER_VERSION, 1, 1785000000000, 0,
            ) + pcm_up)

            received = await asyncio.wait_for(transport.receive_message(), timeout=2)
            assert received.payload["audio"] == pcm_up

            # App -> device: binary PLAY_AUDIO carrying the same PCM back.
            await transport.send_message(create_message(
                MessageType.PLAY_AUDIO,
                {"audio": pcm_up, "sequence_number": 1, "is_final": True},
            ))
            raw_down = await asyncio.wait_for(client.recv(), timeout=2)

            assert isinstance(raw_down, bytes)
            assert raw_down[_PLAY_AUDIO_STRUCT.size:] == pcm_up
            # The measured win: 4800 bytes of PCM used to cost 6561 on the wire.
            assert len(raw_down) == 4800 + _PLAY_AUDIO_STRUCT.size == 4815
        finally:
            await client.close()
            await transport.disconnect()
