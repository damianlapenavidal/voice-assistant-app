"""WebSocket transport implementation for Wi-Fi communication.

Runs a WebSocket server on the laptop. The Raspberry Pi connects as a client.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
import websockets
from websockets.asyncio.server import Server, ServerConnection

from voice_assistant.core.message import Message, create_message, parse_message
from voice_assistant.transport.base import Transport, TransportError

log = structlog.get_logger()

WS_MAX_FRAME_BYTES = 8 * 1024 * 1024


class WebSocketTransport(Transport):
    """WebSocket server transport — accepts a single device connection."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765) -> None:
        self._host = host
        self._port = port
        self._server: Server | None = None
        self._client: ServerConnection | None = None
        self._client_connected: asyncio.Event = asyncio.Event()
        self._serve_task: asyncio.Task[Any] | None = None

    async def start_server(self) -> None:
        """Start the WebSocket server without waiting for a device connection."""
        if self._server is not None:
            return

        self._client_connected.clear()

        self._server = await websockets.serve(
            self._handle_client,
            self._host,
            self._port,
            max_size=WS_MAX_FRAME_BYTES,
        )

        log.info(
            "ws_transport.server_started",
            host=self._host,
            port=self._port,
            address=f"ws://{self._host}:{self._port}",
        )

        log.info(
            "ws_transport.waiting_for_device",
            message=f"Waiting for device connection on ws://{self._host}:{self._port}...",
        )

    async def connect(self) -> None:
        """Start the WebSocket server and wait for a device to connect."""
        if self._server is not None and self._client is not None:
            raise TransportError("Server already running")

        await self.start_server()
        await self._client_connected.wait()

    async def wait_for_client(self) -> None:
        """Wait until a device WebSocket client is connected."""
        if self._client is not None:
            return
        await self._client_connected.wait()

    async def _handle_client(self, connection: ServerConnection) -> None:
        """Handle a newly connected WebSocket client."""
        if self._client is not None:
            log.warning("ws_transport.rejected_extra_client")
            await connection.close(1013, "Only one device connection allowed")
            return

        remote = connection.remote_address
        remote_str = f"{remote[0]}:{remote[1]}" if remote else "unknown"
        self._client = connection
        self._client_connected.set()
        log.info("ws_transport.device_connected", remote_address=remote_str)

        try:
            await connection.wait_closed()
        finally:
            if self._client is connection:
                self._client = None
                self._client_connected.clear()
                log.info(
                    "ws_transport.device_disconnected",
                    remote_address=remote_str,
                )

    async def disconnect(self) -> None:
        """Close the client connection and stop the server."""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
            self._client_connected.clear()

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        log.info("ws_transport.disconnected")

    async def send_message(self, message: Message) -> None:
        """Serialize a Message to JSON and send over WebSocket."""
        if self._client is None:
            raise TransportError("No device connected")

        try:
            data = message.model_dump_json()
            await self._client.send(data)
            log.debug("ws_transport.sent", type=message.type.value)
        except websockets.exceptions.ConnectionClosed as exc:
            self._client = None
            self._client_connected.clear()
            raise TransportError(f"Connection lost while sending: {exc}") from exc

    async def receive_message(self) -> Message:
        """Receive JSON from WebSocket and parse into a Message."""
        if self._client is None:
            raise TransportError("No device connected")

        try:
            raw = await self._client.recv()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            msg = parse_message(raw)
            log.debug("ws_transport.received", type=msg.type.value)
            return msg
        except websockets.exceptions.ConnectionClosed as exc:
            self._client = None
            self._client_connected.clear()
            raise TransportError(f"Connection lost while receiving: {exc}") from exc

    @property
    def is_connected(self) -> bool:
        return self._client is not None
