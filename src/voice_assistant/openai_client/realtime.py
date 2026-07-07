"""OpenAI Realtime API WebSocket client."""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import certifi

import structlog
import websockets
from websockets.asyncio.client import ClientConnection

from voice_assistant.config import Config, DEFAULT_OPENING_GREETING_INSTRUCTIONS
from voice_assistant.audio.utils import SAMPLE_RATE, base64_to_pcm16, pcm16_to_base64
from voice_assistant.audio.vad import VadSettings, derive_vad_settings

log = structlog.get_logger()

REALTIME_BASE_URL = "wss://api.openai.com/v1/realtime"

ConnectFn = Callable[..., Awaitable[ClientConnection]]


class RealtimeClientError(Exception):
    """Base error for Realtime client failures."""


class RealtimeNotConnectedError(RealtimeClientError):
    """Raised when an operation requires an active connection."""


@dataclass(frozen=True)
class RealtimeAudioDelta:
    """PCM16 output audio chunk from the model."""

    pcm_bytes: bytes


@dataclass(frozen=True)
class RealtimeTranscript:
    """Transcript text from user or assistant speech."""

    role: str
    text: str
    final: bool = False


@dataclass(frozen=True)
class RealtimeResponseDone:
    """Signals that the model finished a response."""

    response_id: str | None = None


@dataclass(frozen=True)
class RealtimeResponseCreated:
    """Signals that the model started generating a response."""


@dataclass(frozen=True)
class RealtimeSessionCreated:
    """Signals that the Realtime session was created."""

    session_id: str | None = None


@dataclass(frozen=True)
class RealtimeSessionUpdated:
    """Signals that the Realtime session configuration was applied."""


@dataclass(frozen=True)
class RealtimeErrorEvent:
    """Error event from the Realtime API."""

    message: str
    code: str | None = None


@dataclass(frozen=True)
class RealtimeSpeechStarted:
    """Server VAD detected user started speaking."""


@dataclass(frozen=True)
class RealtimeSpeechStopped:
    """Server VAD detected user stopped speaking."""


RealtimeEvent = (
    RealtimeAudioDelta
    | RealtimeTranscript
    | RealtimeResponseDone
    | RealtimeResponseCreated
    | RealtimeSessionCreated
    | RealtimeSessionUpdated
    | RealtimeErrorEvent
    | RealtimeSpeechStarted
    | RealtimeSpeechStopped
)

OnEventCallback = Callable[[RealtimeEvent], Awaitable[None] | None]


class RealtimeClient:
    """Async WebSocket client for the OpenAI Realtime API."""

    def __init__(
        self,
        *,
        config: Config | None = None,
        api_key: str | None = None,
        model: str | None = None,
        voice: str | None = None,
        instructions: str | None = None,
        vad_settings: VadSettings | None = None,
        on_event: OnEventCallback | None = None,
        connect_fn: ConnectFn | None = None,
    ) -> None:
        cfg = config or Config()
        self._api_key = api_key if api_key is not None else cfg.openai_api_key
        self._model = model if model is not None else cfg.openai_model
        self._voice = voice if voice is not None else cfg.openai_voice
        self._instructions = (
            instructions if instructions is not None else cfg.assistant_instructions
        )
        self._apply_vad_settings(
            vad_settings
            or derive_vad_settings(noise_floor=400.0, user_speech_peak=650.0)
        )
        self._on_event = on_event
        self._connect_fn = connect_fn or websockets.connect

        self._ws: ClientConnection | None = None
        self._receive_task: asyncio.Task[Any] | None = None
        self._event_queue: asyncio.Queue[RealtimeEvent | None] = asyncio.Queue()
        self._connected = False
        self._assistant_transcript_buffer = ""
        self._session_updated_event = asyncio.Event()

    def _apply_vad_settings(self, settings: VadSettings) -> None:
        self._vad_threshold = settings.threshold
        self._vad_silence_ms = settings.silence_ms
        self._vad_prefix_padding_ms = settings.prefix_padding_ms

    @property
    def vad_settings(self) -> VadSettings:
        return VadSettings(
            threshold=self._vad_threshold,
            silence_ms=self._vad_silence_ms,
            prefix_padding_ms=self._vad_prefix_padding_ms,
        )

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None

    async def connect(self, *, send_session_update: bool = True) -> None:
        """Open the Realtime WebSocket and optionally configure the session."""
        if self._connected:
            raise RealtimeClientError("Already connected")

        if not self._api_key:
            raise RealtimeClientError("OPENAI_API_KEY is not configured")

        url = f"{REALTIME_BASE_URL}?model={self._model}"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        ssl_ctx = ssl.create_default_context(cafile=certifi.where())

        log.info("realtime.connecting", model=self._model)
        self._ws = await self._connect_fn(url, additional_headers=headers, ssl=ssl_ctx)
        self._connected = True
        self._receive_task = asyncio.create_task(self._receive_loop())
        if send_session_update:
            await self.update_vad_settings(self.vad_settings)

    async def wait_for_session_updated(self, timeout: float = 10.0) -> None:
        """Wait until OpenAI acknowledges the latest session.update."""
        await asyncio.wait_for(self._session_updated_event.wait(), timeout=timeout)

    async def update_vad_settings(self, settings: VadSettings) -> None:
        """Apply VAD settings and wait for session.updated."""
        if not self.is_connected or self._ws is None:
            raise RealtimeNotConnectedError("Realtime client is not connected")
        self._apply_vad_settings(settings)
        self._session_updated_event.clear()
        await self._send_session_update()
        await self.wait_for_session_updated()

    async def disconnect(self) -> None:
        """Close the WebSocket and stop background tasks."""
        self._connected = False

        if self._receive_task is not None:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        if self._ws is not None:
            await self._ws.close()
            self._ws = None

        self._session_updated_event.clear()
        await self._event_queue.put(None)
        log.info("realtime.disconnected")

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Append PCM16 audio to the Realtime input buffer."""
        if not self.is_connected or self._ws is None:
            raise RealtimeNotConnectedError("Realtime client is not connected")

        event = {
            "type": "input_audio_buffer.append",
            "audio": pcm16_to_base64(pcm_bytes),
        }
        log.debug("realtime.input_audio_append", bytes=len(pcm_bytes))
        await self._ws.send(json.dumps(event))

    async def clear_input_buffer(self) -> None:
        """Discard any audio already in the Realtime input buffer."""
        if not self.is_connected or self._ws is None:
            raise RealtimeNotConnectedError("Realtime client is not connected")
        await self._ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
        log.info("realtime.input_buffer_cleared")

    async def cancel_response(self) -> None:
        """Cancel the in-progress model response."""
        if not self.is_connected or self._ws is None:
            raise RealtimeNotConnectedError("Realtime client is not connected")
        await self._ws.send(json.dumps({"type": "response.cancel"}))
        log.info("realtime.response_cancelled")

    async def commit_input_buffer(self) -> None:
        """Commit pending input audio so server VAD can finalize the user turn."""
        if not self.is_connected or self._ws is None:
            raise RealtimeNotConnectedError("Realtime client is not connected")
        await self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        log.info("realtime.input_buffer_committed")

    async def create_response(self) -> None:
        """Ask the model to respond to committed input audio."""
        if not self.is_connected or self._ws is None:
            raise RealtimeNotConnectedError("Realtime client is not connected")
        event = {
            "type": "response.create",
            "response": {
                "output_modalities": ["audio"],
            },
        }
        await self._ws.send(json.dumps(event))
        log.info("realtime.response_create_requested")

    async def request_opening_greeting(
        self,
        instructions: str | None = None,
    ) -> None:
        """Ask the assistant to speak first with a short opening greeting."""
        if not self.is_connected or self._ws is None:
            raise RealtimeNotConnectedError("Realtime client is not connected")

        greeting_instructions = (
            instructions if instructions is not None else DEFAULT_OPENING_GREETING_INSTRUCTIONS
        )
        event = {
            "type": "response.create",
            "response": {
                "output_modalities": ["audio"],
                "instructions": greeting_instructions,
            },
        }
        await self._ws.send(json.dumps(event))
        log.info("realtime.opening_greeting_requested")

    async def iter_events(self) -> AsyncIterator[RealtimeEvent]:
        """Yield Realtime events until disconnect."""
        while True:
            event = await self._event_queue.get()
            if event is None:
                break
            yield event

    async def _send_session_update(self) -> None:
        if self._ws is None:
            raise RealtimeNotConnectedError("Realtime client is not connected")

        turn_cfg: dict[str, Any] = {
            "type": "server_vad",
            "create_response": True,
            "interrupt_response": True,
            "threshold": self._vad_threshold,
            "prefix_padding_ms": self._vad_prefix_padding_ms,
            "silence_duration_ms": self._vad_silence_ms,
        }

        event = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self._model,
                "instructions": self._instructions,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": turn_cfg,
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                        "voice": self._voice,
                    },
                },
            },
        }
        await self._ws.send(json.dumps(event))
        log.info(
            "realtime.session_update_sent",
            model=self._model,
            voice=self._voice,
            vad_threshold=self._vad_threshold,
            silence_ms=self._vad_silence_ms,
        )

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                event = json.loads(raw)
                await self._handle_server_event(event)
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed as exc:
            log.warning("realtime.connection_closed", code=exc.code, reason=exc.reason)
        except Exception as exc:
            log.error("realtime.receive_loop_error", error=str(exc))
            await self._emit(
                RealtimeErrorEvent(message=str(exc)),
            )
        finally:
            self._connected = False

    async def _handle_server_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "")
        log.debug("realtime.server_event", type=event_type)

        match event_type:
            case "session.created":
                session = event.get("session", {})
                session_id = session.get("id")
                await self._emit(RealtimeSessionCreated(session_id=session_id))

            case "session.updated":
                log.info("realtime.session_updated")
                self._session_updated_event.set()
                await self._emit(RealtimeSessionUpdated())

            case "input_audio_buffer.speech_started":
                log.info("realtime.speech_started")
                await self._emit(RealtimeSpeechStarted())

            case "input_audio_buffer.speech_stopped":
                log.info("realtime.speech_stopped")
                await self._emit(RealtimeSpeechStopped())

            case "response.created":
                log.debug("realtime.response_created")
                await self._emit(RealtimeResponseCreated())

            case "response.output_audio.delta" | "response.audio.delta":
                delta_b64 = event.get("delta", "")
                if delta_b64:
                    await self._emit(
                        RealtimeAudioDelta(pcm_bytes=base64_to_pcm16(delta_b64)),
                    )

            case (
                "response.audio_transcript.delta"
                | "response.output_audio_transcript.delta"
            ):
                delta = event.get("delta", "")
                if delta:
                    self._assistant_transcript_buffer += delta
                    await self._emit(
                        RealtimeTranscript(
                            role="assistant",
                            text=delta,
                            final=False,
                        ),
                    )

            case "response.done":
                if self._assistant_transcript_buffer:
                    await self._emit(
                        RealtimeTranscript(
                            role="assistant",
                            text=self._assistant_transcript_buffer,
                            final=True,
                        ),
                    )
                    self._assistant_transcript_buffer = ""
                response = event.get("response", {})
                await self._emit(
                    RealtimeResponseDone(response_id=response.get("id")),
                )

            case "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript", "")
                if transcript:
                    await self._emit(
                        RealtimeTranscript(role="user", text=transcript, final=True),
                    )

            case "error":
                error = event.get("error", {})
                await self._emit(
                    RealtimeErrorEvent(
                        message=error.get("message", "Unknown Realtime API error"),
                        code=error.get("code"),
                    ),
                )

    async def _emit(self, event: RealtimeEvent) -> None:
        await self._event_queue.put(event)
        if self._on_event is not None:
            result = self._on_event(event)
            if asyncio.iscoroutine(result):
                await result
