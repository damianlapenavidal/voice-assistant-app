"""WebSocket transport implementation for Wi-Fi communication.

Runs a WebSocket server on the laptop. The Raspberry Pi connects as a client.
"""

from __future__ import annotations

import asyncio
import struct
from datetime import datetime, timezone
from typing import Any

import structlog
import websockets
from websockets.asyncio.server import Server, ServerConnection

from voice_assistant.audio.utils import base64_to_pcm16, pcm16_to_base64
from voice_assistant.core.message import (
    Message,
    MessageType,
    create_message,
    parse_message,
)
from voice_assistant.transport.base import Transport, TransportError

log = structlog.get_logger()

WS_MAX_FRAME_BYTES = 8 * 1024 * 1024

# Binary AUDIO_FRAME/PLAY_AUDIO framing -- see docs/protocol.md's "Binary
# Audio Framing" section. Only used once the device negotiates
# "binary_audio"; the reserved trailing u32 in each header is earmarked for a
# future barge-in phase and must stay 0 until then.
AUDIO_FRAME_TAG = 0x01
PLAY_AUDIO_TAG = 0x02
HEADER_VERSION = 1
_AUDIO_FRAME_STRUCT = struct.Struct(">BBIQI")  # tag, version, seq, ts_ms, reserved
_PLAY_AUDIO_STRUCT = struct.Struct(">BBIBII")  # tag, version, seq, flags, duration_ms, reserved
_FLAG_IS_FINAL = 0x01
_DURATION_UNKNOWN = 0xFFFFFFFF


class WebSocketTransport(Transport):
    """WebSocket server transport — accepts a single device connection."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765) -> None:
        self._host = host
        self._port = port
        self._server: Server | None = None
        self._client: ServerConnection | None = None
        self._client_connected: asyncio.Event = asyncio.Event()
        self._serve_task: asyncio.Task[Any] | None = None
        self._binary_audio_enabled = False

    def set_binary_audio_enabled(self, enabled: bool) -> None:
        """Choose the wire format for outbound PLAY_AUDIO on this connection.

        Set once per connection from the HELLO/HELLO_ACK negotiation. Only
        affects sending: inbound framing is whatever the device chose, which
        its frame type already tells us.
        """
        self._binary_audio_enabled = enabled
        log.info("ws_transport.binary_audio", enabled=enabled)

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
        """Send a Message, as a binary frame for negotiated PLAY_AUDIO and
        JSON for everything else."""
        if self._client is None:
            raise TransportError("No device connected")

        try:
            if self._binary_audio_enabled and message.type == MessageType.PLAY_AUDIO:
                data: str | bytes = _encode_play_audio_binary(message)
            else:
                data = _json_with_encoded_audio(message)
            await self._client.send(data)
            log.debug("ws_transport.sent", type=message.type.value, bytes=len(data))
        except websockets.exceptions.ConnectionClosed as exc:
            self._client = None
            self._client_connected.clear()
            raise TransportError(f"Connection lost while sending: {exc}") from exc

    async def receive_message(self) -> Message:
        """Receive a Message, decoding binary AUDIO_FRAMEs and parsing
        everything else as JSON.

        Dispatch keys off the frame type rather than the negotiated flag: what
        arrives reflects the device's choice, not ours.
        """
        if self._client is None:
            raise TransportError("No device connected")

        try:
            raw = await self._client.recv()
            msg = (
                _decode_audio_frame_binary(raw)
                if isinstance(raw, (bytes, bytearray))
                else parse_message(raw)
            )
            log.debug("ws_transport.received", type=msg.type.value)
            return msg
        except websockets.exceptions.ConnectionClosed as exc:
            self._client = None
            self._client_connected.clear()
            raise TransportError(f"Connection lost while receiving: {exc}") from exc

    @property
    def is_connected(self) -> bool:
        return self._client is not None


def _json_with_encoded_audio(message: Message) -> str:
    """Serialize to JSON, base64-ing a raw-bytes `audio` field first.

    Producers hand the transport raw PCM so the binary path can ship it
    untouched. Pydantic would decode those bytes into a mangled string rather
    than base64 them, so the JSON path has to encode explicitly -- otherwise a
    device on the JSON path receives silently corrupted audio.
    """
    payload = message.payload
    if payload is not None and isinstance(payload.get("audio"), (bytes, bytearray)):
        message = message.model_copy(
            update={"payload": {**payload, "audio": pcm16_to_base64(payload["audio"])}},
        )
    return message.model_dump_json()


def _encode_play_audio_binary(message: Message) -> bytes:
    """Pack a PLAY_AUDIO message into a binary frame (header + raw PCM)."""
    payload = message.payload or {}
    audio = payload.get("audio") or b""
    pcm = audio if isinstance(audio, (bytes, bytearray)) else base64_to_pcm16(audio)
    duration = payload.get("duration_ms")
    header = _PLAY_AUDIO_STRUCT.pack(
        PLAY_AUDIO_TAG,
        HEADER_VERSION,
        int(payload.get("sequence_number") or 0) & 0xFFFFFFFF,
        _FLAG_IS_FINAL if payload.get("is_final") else 0,
        _DURATION_UNKNOWN if duration is None else int(duration) & 0xFFFFFFFF,
        0,
    )
    return header + bytes(pcm)


def _decode_audio_frame_binary(raw: bytes) -> Message:
    """Unpack a binary AUDIO_FRAME into the same Message a JSON one produces.

    A malformed frame becomes a recoverable ERROR rather than an exception: a
    raise here reads as connection loss to the session's receive loop, which
    would tear down a live session over one bad audio chunk.
    """
    try:
        if len(raw) < 2:
            raise ValueError("frame shorter than 2-byte tag+version prefix")
        tag, version = raw[0], raw[1]
        if tag != AUDIO_FRAME_TAG:
            raise ValueError(f"unexpected binary frame tag {tag:#04x}")
        if version != HEADER_VERSION:
            raise ValueError(f"unsupported AUDIO_FRAME header version {version}")
        if len(raw) < _AUDIO_FRAME_STRUCT.size:
            raise ValueError("truncated AUDIO_FRAME header")

        _, _, seq, capture_ms, _reserved = _AUDIO_FRAME_STRUCT.unpack_from(raw, 0)
        capture_iso = datetime.fromtimestamp(
            capture_ms / 1000, tz=timezone.utc,
        ).isoformat()
        return Message(
            type=MessageType.AUDIO_FRAME,
            payload={
                "audio": bytes(raw[_AUDIO_FRAME_STRUCT.size:]),
                "sequence_number": seq,
                "timestamp": capture_iso,
            },
        )
    except (struct.error, ValueError, IndexError, OSError) as exc:
        log.warning(
            "ws_transport.malformed_binary_frame",
            reason=str(exc),
            frame_bytes=len(raw),
        )
        return create_message(
            MessageType.ERROR,
            {
                "code": "MALFORMED_AUDIO_FRAME",
                "message": str(exc),
                "recoverable": True,
            },
        )
