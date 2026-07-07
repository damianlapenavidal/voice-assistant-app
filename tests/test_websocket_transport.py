"""Tests for WebSocket transport: server start, connect, send/receive, disconnect."""

import asyncio

import pytest
import websockets

from voice_assistant.core.message import MessageType, create_message, parse_message
from voice_assistant.transport.base import TransportError
from voice_assistant.transport.websocket_transport import WebSocketTransport

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
