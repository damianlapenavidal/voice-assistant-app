"""Session manager orchestrating device and AI sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from enum import Enum, auto
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from voice_assistant.config import Config
from voice_assistant.core.message import (
    Message,
    MessageType,
    create_message,
)
from voice_assistant.transport.base import Transport, TransportError

if TYPE_CHECKING:
    from voice_assistant.audio.bridge import AudioBridge

log = structlog.get_logger()

EventListener = Callable[[str, dict[str, Any]], None]


class SessionState(Enum):
    IDLE = auto()
    CONNECTING = auto()
    ACTIVE = auto()
    STREAMING = auto()
    SHUTDOWN = auto()


class SessionManager:
    """Manages the lifecycle of a device session over a given Transport."""

    def __init__(
        self,
        transport: Transport,
        max_iterations: int = 20,
        *,
        loopback: bool = True,
        config: Config | None = None,
    ) -> None:
        self._transport = transport
        self._max_iterations = max_iterations
        self._loopback = loopback
        self._config = config or Config()
        self._state = SessionState.IDLE
        self._session_id: str | None = None
        self._event_listeners: list[EventListener] = []
        self._receive_task: asyncio.Task[None] | None = None
        self._audio_bridge: AudioBridge | None = None
        self._handshake_complete = False
        self._calibration_metrics: dict[str, Any] | None = None

    @property
    def transport(self) -> Transport:
        return self._transport

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def config(self) -> Config:
        return self._config

    @property
    def is_openai_configured(self) -> bool:
        return bool(self._config.openai_api_key)

    @property
    def active_mode(self) -> str:
        """Return the active audio mode: 'openai', 'loopback', or 'idle'."""
        if self._audio_bridge is not None:
            return self._audio_bridge.mode
        if self._loopback:
            return "loopback"
        return "openai" if self.is_openai_configured else "loopback"

    @property
    def handshake_complete(self) -> bool:
        return self._handshake_complete

    @property
    def conversation_state(self) -> str:
        if self._audio_bridge is not None:
            return self._audio_bridge.conversation_state
        return "idle"

    def add_event_listener(self, listener: EventListener) -> None:
        self._event_listeners.append(listener)

    def _emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        for listener in self._event_listeners:
            try:
                listener(event, data or {})
            except Exception:
                pass

    async def start_device_server(self) -> None:
        """Start the transport listener without blocking for a device connection."""
        self._state = SessionState.CONNECTING
        start_server = getattr(self._transport, "start_server", None)
        if start_server is not None:
            await start_server()
        else:
            await self._transport.connect()

    async def wait_for_device(self) -> None:
        """Connect transport, complete HELLO handshake, and stay in ACTIVE."""
        await self.start_device_server()
        await self._await_handshake()

    async def _await_handshake(self) -> None:
        """Wait for a device socket and complete HELLO → HELLO_ACK."""
        wait_for_client = getattr(self._transport, "wait_for_client", None)
        if wait_for_client is not None:
            await wait_for_client()

        hello = await self._transport.receive_message()
        if hello.type != MessageType.HELLO:
            raise TransportError(f"Expected HELLO, got {hello.type.value}")

        await self._complete_handshake(hello)

    async def _complete_handshake(self, hello: Message) -> None:
        self._session_id = str(uuid4())
        ack = create_message(
            MessageType.HELLO_ACK,
            {
                "session_id": self._session_id,
                "audio_config": {
                    "sample_rate": 24000,
                    "format": "pcm16",
                    "channels": 1,
                },
            },
        )
        await self._transport.send_message(ack)

        self._handshake_complete = True
        self._calibration_metrics = None
        self._state = SessionState.ACTIVE
        device_id = hello.payload.get("device_id") if hello.payload else None
        log.info(
            "session.device_ready",
            session_id=self._session_id,
            device_id=device_id,
        )
        self._emit("session_started", {
            "session_id": self._session_id,
            "device_id": device_id,
            "device_info": hello.payload,
        })

    async def start_conversation(self) -> None:
        """Send START_AUDIO_STREAM and transition to STREAMING."""
        if not self._handshake_complete:
            raise TransportError("Cannot start conversation before HELLO_ACK")
        if self._state != SessionState.ACTIVE:
            raise TransportError(f"Cannot start conversation in state {self._state.name}")

        use_loopback = self._loopback or not self.is_openai_configured
        from voice_assistant.audio.bridge import AudioBridge

        resuming = self._calibration_metrics is not None
        self._audio_bridge = AudioBridge(
            self._transport,
            loopback=use_loopback,
            config=self._config,
        )
        self._audio_bridge.set_transcript_callback(self._on_transcript)
        self._audio_bridge.set_mic_mute_callback(self._on_mic_mute)
        self._audio_bridge.set_conversation_state_callback(self._on_conversation_state)
        self._audio_bridge.set_device_ready(True)

        if use_loopback:
            if resuming:
                self._audio_bridge.start_resume()
            else:
                self._audio_bridge.start()
        elif resuming:
            await self._audio_bridge.resume_async(self._calibration_metrics)
        else:
            await self._audio_bridge.start_async()

        start_payload = {"skip_calibration": True} if resuming else None
        msg = create_message(MessageType.START_AUDIO_STREAM, start_payload)
        await self._transport.send_message(msg)
        self._state = SessionState.STREAMING
        log.info(
            "session.conversation_started",
            session_id=self._session_id,
            mode=self._audio_bridge.mode,
            resumed=resuming,
        )
        self._emit("conversation_started", {
            "session_id": self._session_id,
            "mode": self._audio_bridge.mode,
            "resumed": resuming,
        })

    async def stop_conversation(self) -> None:
        """Send STOP_AUDIO_STREAM and return to ACTIVE."""
        if self._state != SessionState.STREAMING:
            raise TransportError(f"Cannot stop conversation in state {self._state.name}")

        if self._audio_bridge is not None:
            if self._audio_bridge.loopback:
                self._audio_bridge.stop()
            else:
                await self._audio_bridge.stop_async()
            self._audio_bridge.set_device_ready(False)
            self._audio_bridge = None

        msg = create_message(MessageType.STOP_AUDIO_STREAM)
        await self._transport.send_message(msg)
        self._state = SessionState.ACTIVE
        log.info("session.conversation_stopped", session_id=self._session_id)
        self._emit("conversation_stopped", {"session_id": self._session_id})

    def _on_transcript(self, role: str, text: str, final: bool) -> None:
        self._emit("transcript", {"role": role, "text": text, "final": final})

    def _on_mic_mute(self, muted: bool) -> None:
        self._emit("mic_muted", {"muted": muted})

    def _on_conversation_state(self, state: str) -> None:
        self._emit("conversation_state", {"state": state})

    async def shutdown_device(self) -> None:
        """Send SHUTDOWN_DEVICE and disconnect."""
        if self._state == SessionState.SHUTDOWN:
            return

        await self.stop_receive_loop()

        try:
            msg = create_message(MessageType.SHUTDOWN_DEVICE)
            await self._transport.send_message(msg)
        except TransportError:
            pass
        await self._transport.disconnect()
        self._state = SessionState.SHUTDOWN
        log.info("session.shutdown", session_id=self._session_id)
        self._emit("session_shutdown", {"session_id": self._session_id})

    def start_receive_loop(self) -> None:
        """Start the background message receive loop."""
        if self._receive_task is not None and not self._receive_task.done():
            return
        self._receive_task = asyncio.create_task(
            self._run_receive_loop(),
            name="session-receive-loop",
        )

    async def stop_receive_loop(self) -> None:
        """Stop the background message receive loop."""
        if self._receive_task is None:
            return
        self._receive_task.cancel()
        try:
            await self._receive_task
        except asyncio.CancelledError:
            pass
        self._receive_task = None

    async def _handle_device_disconnect(self, error: str | None = None) -> None:
        """Reset session state after the device socket is lost."""
        log.warning("session.receive_loop_ended", error=error)

        if self._audio_bridge is not None:
            await self._audio_bridge.reset_on_disconnect()
            self._audio_bridge = None

        previous_session_id = self._session_id
        self._handshake_complete = False
        self._session_id = None
        self._calibration_metrics = None

        if self._state == SessionState.STREAMING:
            self._state = SessionState.CONNECTING
        elif self._state != SessionState.SHUTDOWN:
            self._state = SessionState.CONNECTING

        self._emit("device_disconnected", {"session_id": previous_session_id})

    async def _run_receive_loop(self) -> None:
        """Background loop: receive and process messages while device is connected."""
        iteration = 0
        while self._state != SessionState.SHUTDOWN:
            try:
                if not self._handshake_complete:
                    await self._await_handshake()
                while True:
                    msg = await self._transport.receive_message()
                    iteration += 1
                    if not await self._process_message(msg, iteration):
                        return
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except TransportError as exc:
                if self._state == SessionState.SHUTDOWN:
                    return
                await self._handle_device_disconnect(str(exc))
                try:
                    await self._await_handshake()
                except asyncio.CancelledError:
                    raise
                except TransportError as reconnect_exc:
                    log.warning(
                        "session.reconnect_handshake_failed",
                        error=str(reconnect_exc),
                    )
                    await asyncio.sleep(0.5)

    async def _process_message(self, msg: Message, iteration: int) -> bool:
        """Process a received message. Returns False if loop should break."""
        payload = msg.payload or {}

        match msg.type:
            case MessageType.HELLO:
                if self._handshake_complete:
                    log.info("session.hello_on_reconnect")
                await self._complete_handshake(msg)
            case MessageType.AUDIO_FRAME:
                if self._audio_bridge is not None:
                    await self._audio_bridge.handle_audio_frame(payload)
                self._emit("audio_frame", payload)
                log.debug(
                    "session.audio_frame",
                    seq=payload.get("sequence_number"),
                    audio_bytes=len(payload.get("audio", "")),
                    iteration=iteration,
                )
            case MessageType.DEVICE_STATUS:
                log.info(
                    "session.device_status",
                    battery=payload.get("battery_percent"),
                    cpu_temp=payload.get("cpu_temp"),
                    recording=payload.get("is_recording"),
                )
                self._emit("device_status", payload)
            case MessageType.ERROR:
                log.error(
                    "session.device_error",
                    code=payload.get("code"),
                    error_message=payload.get("message"),
                    recoverable=payload.get("recoverable"),
                )
                self._emit("device_error", payload)
                if not payload.get("recoverable", True):
                    return False
            case MessageType.PLAYBACK_COMPLETE:
                log.info(
                    "session.playback_complete",
                    seq=payload.get("sequence_number"),
                    duration_ms=payload.get("duration_ms"),
                )
                if self._audio_bridge is not None:
                    await self._audio_bridge.handle_playback_complete(payload)
                self._emit("playback_complete", payload)
            case MessageType.CALIBRATION_STATUS:
                if self._calibration_metrics is not None:
                    log.debug("session.calibration_status_ignored_resume")
                elif self._audio_bridge is not None:
                    await self._audio_bridge.handle_calibration_status(payload)
                    self._emit("calibration_status", payload)
            case MessageType.CALIBRATION_COMPLETE:
                hello_audio = payload.get("hello_audio") or ""
                if self._calibration_metrics is not None:
                    log.debug("session.calibration_complete_ignored_resume")
                else:
                    log.info(
                        "session.calibration_complete",
                        **{
                            k: v
                            for k, v in payload.items()
                            if k != "hello_audio"
                        },
                        hello_audio_bytes=len(hello_audio),
                    )
                    if self._audio_bridge is not None:
                        calibrated = await self._audio_bridge.handle_calibration_complete(
                            payload,
                        )
                        if calibrated:
                            self._calibration_metrics = {
                                k: v
                                for k, v in payload.items()
                                if k != "hello_audio"
                            }
                    self._emit("calibration_complete", payload)
            case _:
                log.debug(
                    "session.message_received",
                    type=msg.type.value,
                )
                self._emit("message", {"type": msg.type.value, **payload})
        return True

    async def run_session_loop(self) -> None:
        """Run a complete mock session cycle (for tests).

        Performs handshake, streams for max_iterations messages, stops streaming,
        then shuts down. Does not use the background receive loop.
        """
        try:
            await self.wait_for_device()
            await self.start_conversation()

            continuous = self._max_iterations <= 0
            iteration = 0

            while continuous or iteration < self._max_iterations:
                iteration += 1
                msg = await self._transport.receive_message()
                if not await self._process_message(msg, iteration):
                    break

            await self.stop_conversation()
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("session.interrupted")
        finally:
            await self.shutdown_device()
