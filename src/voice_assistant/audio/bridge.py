"""AudioBridge: relay audio frames between device and AI pipeline."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import structlog

from voice_assistant.audio.utils import (
    OPENING_NUDGE_WAIT_SEC,
    PLAY_AUDIO_CHUNK_BYTES,
    base64_to_pcm16,
    compute_recovery_ms,
    is_meaningful_user_text,
    is_valid_calibration_hello_transcript,
    likely_calibration_prompt_transcript,
    likely_echo_transcript,
    pcm16_to_base64,
    trim_calibration_hello_audio,
)
from voice_assistant.core.message import MessageType, create_message
from voice_assistant.transport.base import Transport

log = structlog.get_logger()

SAMPLE_RATE = 24000
BYTES_PER_SAMPLE = 2
BYTE_RATE = SAMPLE_RATE * BYTES_PER_SAMPLE
PLAYBACK_RECOVERY_MS = 300
UNMUTE_SAFETY_MARGIN_MS = 1000
STARTUP_RESPONSE_TIMEOUT_SEC = 15.0

TranscriptCallback = Callable[[str, str, bool], None]
MicMuteCallback = Callable[[bool], None]
ConversationStateCallback = Callable[[str], None]


class AudioBridge:
    """Routes incoming AUDIO_FRAMEs to either a loopback echo or an AI pipeline.

    In loopback mode the bridge immediately sends each frame back as a
    PLAY_AUDIO message — useful for verifying the full audio round-trip
    before OpenAI integration is wired up.

    In OpenAI mode the bridge forwards audio to a RealtimeClient and
    relays AI-generated audio back as PLAY_AUDIO messages.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        loopback: bool = True,
        config: Any | None = None,
    ) -> None:
        self._transport = transport
        self._loopback = loopback
        self._config = config
        self._running = False
        self._frame_count = 0
        self._realtime_client: Any | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._transcript_callback: TranscriptCallback | None = None
        self._mic_mute_callback: MicMuteCallback | None = None
        self._conversation_state_callback: ConversationStateCallback | None = None
        self._conversation_state = "idle"
        self._mic_muted = False
        self._awaiting_calibration = False
        self._calibration_phase: str | None = None
        self._awaiting_opening_greeting = False

        self._audio_buffer = bytearray()
        self._audio_seq = 0
        self._buffer_lock = asyncio.Lock()
        self._ai_speaking = False
        self._pending_playback_seq: int | None = None
        self._unmute_timeout_task: asyncio.Task[None] | None = None
        self._device_ready = False
        self._chunks_sent_this_response = 0
        self._response_duration_ms = 0
        self._recovery_until = 0.0
        self._conversation_armed = False
        self._last_assistant_text = ""
        self._awaiting_user_transcript = False
        self._buffered_transcripts: list[tuple[str, str, bool]] = []
        self._opening_phase_active = False
        self._opening_nudge_sent = False
        self._explicit_greeting_pending = False
        self._pending_greeting_playback = False
        self._opening_nudge_task: asyncio.Task[None] | None = None
        self._realtime_connect_task: asyncio.Task[None] | None = None
        self._session_ready = False
        self._calibration_hello_pcm: bytes | None = None
        self._calibration_hello_injected = False
        self._awaiting_first_calibration_playback = False
        self._calibration_user_transcript_emitted = False
        self._startup_response_started = False
        self._ignore_audio_bytes_remaining = 0
        self._startup_response_timeout_task: asyncio.Task[None] | None = None

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def loopback(self) -> bool:
        return self._loopback

    @property
    def mode(self) -> str:
        return "loopback" if self._loopback else "openai"

    @property
    def conversation_state(self) -> str:
        return self._conversation_state

    @property
    def mic_muted(self) -> bool:
        return self._mic_muted

    @property
    def calibration_phase(self) -> str | None:
        return self._calibration_phase

    def set_transcript_callback(self, callback: TranscriptCallback | None) -> None:
        self._transcript_callback = callback

    def set_mic_mute_callback(self, callback: MicMuteCallback | None) -> None:
        self._mic_mute_callback = callback

    def set_conversation_state_callback(
        self,
        callback: ConversationStateCallback | None,
    ) -> None:
        self._conversation_state_callback = callback

    def _emit_transcript(self, role: str, text: str, final: bool) -> None:
        if self._transcript_callback is not None:
            self._transcript_callback(role, text, final)

    def _flush_buffered_transcripts(self) -> None:
        """Emit any assistant transcripts held until user text arrives."""
        for role, text, final in self._buffered_transcripts:
            self._emit_transcript(role, text, final)
        self._buffered_transcripts.clear()

    def _handle_transcript(self, role: str, text: str, final: bool) -> None:
        """Emit transcripts in conversational order (user before assistant).

        OpenAI often delivers the assistant transcript before async user
        transcription completes; buffer assistant lines until the user turn lands.
        """
        if role == "assistant" and self._awaiting_user_transcript:
            self._buffered_transcripts.append((role, text, final))
            return

        if role == "user" and final:
            self._awaiting_user_transcript = False
            self._emit_transcript(role, text, final)
            self._flush_buffered_transcripts()
            return

        self._emit_transcript(role, text, final)

    def _reset_opening_phase(self) -> None:
        self._opening_phase_active = False
        self._opening_nudge_sent = False
        self._explicit_greeting_pending = False
        self._pending_greeting_playback = False
        self._cancel_opening_nudge_task()

    def _cancel_opening_nudge_task(self) -> None:
        if self._opening_nudge_task is not None:
            self._opening_nudge_task.cancel()
            self._opening_nudge_task = None

    def _arm_conversation(self) -> None:
        if self._conversation_armed:
            return
        self._conversation_armed = True
        self._opening_phase_active = False
        self._cancel_opening_nudge_task()
        log.info("audio_bridge.conversation_armed")

    def _reset_transcript_ordering(self) -> None:
        self._awaiting_user_transcript = False
        self._buffered_transcripts.clear()

    def _set_conversation_state(self, state: str) -> None:
        self._conversation_state = state
        if self._conversation_state_callback is not None:
            try:
                self._conversation_state_callback(state)
            except Exception:
                pass

    def _cancel_startup_response_timeout(self) -> None:
        if self._startup_response_timeout_task is not None:
            self._startup_response_timeout_task.cancel()
            self._startup_response_timeout_task = None

    def _schedule_startup_response_timeout(self) -> None:
        """Recover if the first calibration-hello response never starts."""
        self._cancel_startup_response_timeout()

        async def _timeout() -> None:
            try:
                await asyncio.sleep(STARTUP_RESPONSE_TIMEOUT_SEC)
                if (
                    self._running
                    and not self._ai_speaking
                    and not self._startup_response_started
                    and self._realtime_client is not None
                    and (
                        self._conversation_state == "processing"
                        or self._awaiting_first_calibration_playback
                    )
                ):
                    log.warning("audio_bridge.startup_response_timeout")
                    await self._realtime_client.create_response()
            except asyncio.CancelledError:
                raise

        self._startup_response_timeout_task = asyncio.create_task(
            _timeout(),
            name="audio-bridge-startup-response-timeout",
        )

    def _reset_calibration_hello_state(self) -> None:
        self._calibration_hello_pcm = None
        self._calibration_hello_injected = False
        self._awaiting_first_calibration_playback = False
        self._calibration_user_transcript_emitted = False
        self._startup_response_started = False
        self._ignore_audio_bytes_remaining = 0
        self._cancel_startup_response_timeout()

    def _should_ignore_live_speech_vad(self) -> bool:
        """Ignore server VAD for injected calibration/opening audio, not live mic."""
        if self._ai_speaking:
            return True
        if time.monotonic() < self._recovery_until:
            return True
        if self._awaiting_first_calibration_playback:
            return True
        if self._calibration_hello_injected and self._awaiting_user_transcript:
            return True
        if self._awaiting_opening_greeting:
            return True
        return False

    def set_device_ready(self, ready: bool) -> None:
        """Gate device commands until HELLO → HELLO_ACK completes."""
        self._device_ready = ready

    @property
    def device_ready(self) -> bool:
        return self._device_ready

    def start(self) -> None:
        self._running = True
        self._frame_count = 0
        self._set_conversation_state("calibrating")
        self._calibration_phase = "quiet"
        log.info("audio_bridge.started", loopback=self._loopback)

    async def start_async(self) -> None:
        """Start the bridge; begin OpenAI connect while the device calibrates."""
        self.start()
        if not self._loopback:
            self._awaiting_calibration = True
            log.info("audio_bridge.awaiting_calibration")
            self._realtime_connect_task = asyncio.create_task(
                self._early_realtime_connect(),
                name="audio-bridge-realtime-connect",
            )

    def start_resume(self) -> None:
        """Resume streaming without re-calibration (loopback)."""
        self._running = True
        self._frame_count = 0
        self._awaiting_calibration = False
        self._calibration_phase = None
        self._conversation_armed = True
        self._set_conversation_state("listening")
        log.info("audio_bridge.resumed", loopback=self._loopback)

    async def resume_async(self, calibration_metrics: dict) -> None:
        """Resume OpenAI conversation using cached device calibration."""
        from voice_assistant.audio.vad import derive_vad_settings

        self._running = True
        self._frame_count = 0
        self._awaiting_calibration = False
        self._calibration_phase = None
        self._conversation_armed = True

        if self._loopback:
            self._set_conversation_state("listening")
            log.info("audio_bridge.resumed", loopback=True)
            return

        noise_floor = float(calibration_metrics.get("noise_floor", 400.0))
        user_speech_peak = float(calibration_metrics.get("user_speech_peak", 650.0))
        vad_settings = derive_vad_settings(
            noise_floor=noise_floor,
            user_speech_peak=user_speech_peak,
        )
        self._set_conversation_state("connecting_openai")
        log.info("audio_bridge.resuming_with_cached_calibration")

        await self._ensure_realtime_connected(vad_settings=vad_settings)
        if self._realtime_client is not None:
            await self._realtime_client.clear_input_buffer()
        self._set_conversation_state("listening")
        await self._send_mute(False)

    def stop(self) -> None:
        self._running = False
        self._set_conversation_state("idle")
        self._awaiting_calibration = False
        self._calibration_phase = None
        self._awaiting_opening_greeting = False
        self._conversation_armed = False
        self._reset_opening_phase()
        self._reset_transcript_ordering()
        self._reset_calibration_hello_state()
        log.info(
            "audio_bridge.stopped",
            frames_processed=self._frame_count,
        )

    async def stop_async(self) -> None:
        """Stop the bridge and disconnect from OpenAI if connected."""
        self._cancel_unmute_timeout()
        self._cancel_opening_nudge_task()
        self._cancel_startup_response_timeout()
        if self._mic_muted and self._device_ready:
            await self._send_mute(False)
        self.stop()
        self._reset_calibration_hello_state()
        await self._disconnect_realtime()

    async def reset_on_disconnect(self) -> None:
        """Tear down playback/OpenAI state without sending device commands."""
        self._cancel_unmute_timeout()
        self._device_ready = False
        self._pending_playback_seq = None
        self._ai_speaking = False
        self._mic_muted = False
        self._chunks_sent_this_response = 0
        self._response_duration_ms = 0
        self._recovery_until = 0.0
        self._conversation_armed = False
        self._session_ready = False
        self._cancel_realtime_connect_task()
        self._reset_opening_phase()
        async with self._buffer_lock:
            self._audio_buffer.clear()
        await self._disconnect_realtime()
        self._set_conversation_state("idle")
        self._awaiting_calibration = False
        self._calibration_phase = None
        self._awaiting_opening_greeting = False
        self._running = False
        self._reset_transcript_ordering()
        self._reset_calibration_hello_state()
        log.info("audio_bridge.reset_on_disconnect")

    async def handle_audio_frame(self, payload: dict) -> None:
        """Process an incoming AUDIO_FRAME payload.

        In loopback mode the same audio data is sent back as PLAY_AUDIO.
        In OpenAI mode the audio is forwarded to the RealtimeClient.
        """
        if not self._running:
            return

        if self._awaiting_calibration:
            return

        if self._awaiting_opening_greeting:
            return

        if self._awaiting_first_calibration_playback:
            log.debug("audio_bridge.live_audio_blocked_until_first_reply")
            return

        audio_b64 = payload.get("audio", "")
        if (
            not self._loopback
            and self._calibration_hello_injected
            and self._ignore_audio_bytes_remaining > 0
            and audio_b64
        ):
            pcm_bytes = base64_to_pcm16(audio_b64)
            if len(pcm_bytes) <= self._ignore_audio_bytes_remaining:
                self._ignore_audio_bytes_remaining -= len(pcm_bytes)
                log.debug(
                    "audio_bridge.calibration_hello_duplicate_ignored",
                    bytes=len(pcm_bytes),
                    remaining=self._ignore_audio_bytes_remaining,
                )
                return
            skip = self._ignore_audio_bytes_remaining
            self._ignore_audio_bytes_remaining = 0
            pcm_bytes = pcm_bytes[skip:]
            if self._realtime_client is not None and self._realtime_client.is_connected:
                await self._realtime_client.send_audio(pcm_bytes)
            self._frame_count += 1
            return

        t_start = time.monotonic()
        self._frame_count += 1
        if self._frame_count == 1:
            log.info("audio_bridge.user_audio_stream_started")

        if self._loopback:
            play_msg = create_message(
                MessageType.PLAY_AUDIO,
                {
                    "audio": payload.get("audio", ""),
                    "sequence_number": payload.get("sequence_number", 0),
                },
            )
            await self._transport.send_message(play_msg)
        elif self._realtime_client is not None and self._realtime_client.is_connected:
            if audio_b64:
                pcm_bytes = base64_to_pcm16(audio_b64)
                log.debug(
                    "audio_bridge.forwarding_audio",
                    seq=payload.get("sequence_number"),
                    bytes=len(pcm_bytes),
                )
                await self._realtime_client.send_audio(pcm_bytes)

        elapsed_ms = (time.monotonic() - t_start) * 1000
        log.debug(
            "audio_bridge.frame_handled",
            seq=payload.get("sequence_number"),
            loopback=self._loopback,
            latency_ms=round(elapsed_ms, 2),
            frame_count=self._frame_count,
        )

    async def handle_playback_complete(self, payload: dict) -> None:
        """Unmute mic after the device finishes playing a final response chunk."""
        seq = payload.get("sequence_number")
        if self._pending_playback_seq is not None and seq != self._pending_playback_seq:
            log.debug(
                "audio_bridge.playback_complete_stale",
                expected=self._pending_playback_seq,
                got=seq,
            )
            return

        self._cancel_unmute_timeout()
        self._pending_playback_seq = None

        if self._mic_muted:
            self._ai_speaking = False
            playback_ms = int(payload.get("duration_ms") or self._response_duration_ms)
            recovery_ms = compute_recovery_ms(playback_ms)
            self._recovery_until = time.monotonic() + recovery_ms / 1000.0
            await self._send_mute(False)
            log.info(
                "audio_bridge.unmuted_after_playback",
                seq=seq,
                duration_ms=playback_ms,
                recovery_ms=recovery_ms,
            )
            if self._pending_greeting_playback:
                self._pending_greeting_playback = False
                if (
                    self._opening_phase_active
                    and not self._conversation_armed
                    and not self._opening_nudge_sent
                ):
                    self._set_conversation_state("waiting_for_kid")
                    self._schedule_opening_nudge()
            elif self._awaiting_first_calibration_playback:
                self._awaiting_first_calibration_playback = False
                if self._calibration_hello_pcm:
                    self._ignore_audio_bytes_remaining = len(self._calibration_hello_pcm)
                self._recovery_until = time.monotonic() + 1.5
                self._calibration_hello_injected = False
                self._set_conversation_state("listening")
            elif self._conversation_armed:
                self._set_conversation_state("listening")
            elif self._opening_phase_active:
                self._set_conversation_state("waiting_for_kid")
            else:
                self._set_conversation_state("listening")

    async def handle_calibration_status(self, payload: dict) -> None:
        """Update UI when the device moves between calibration phases."""
        phase = payload.get("phase")
        if phase:
            self._calibration_phase = phase
            self._set_conversation_state(f"calibrating_{phase}")
            log.info("audio_bridge.calibration_status", phase=phase)

    async def handle_calibration_complete(self, payload: dict) -> bool:
        """Connect to OpenAI using VAD settings derived from device calibration."""
        from voice_assistant.audio.vad import derive_vad_settings

        noise_floor = float(payload.get("noise_floor", 400.0))
        user_speech_peak = float(payload.get("user_speech_peak", 650.0))
        if not payload.get("speech_detected", True):
            log.error("audio_bridge.calibration_rejected", reason="no_speech_detected")
            self._set_conversation_state("calibrating_retry")
            return False

        voice_margin = user_speech_peak - noise_floor
        if voice_margin < 80.0:
            log.error(
                "audio_bridge.calibration_rejected",
                reason="voice_too_quiet",
                margin=voice_margin,
            )
            self._set_conversation_state("calibrating_retry")
            return False

        vad_settings = derive_vad_settings(
            noise_floor=noise_floor,
            user_speech_peak=user_speech_peak,
        )
        hello_b64 = payload.get("hello_audio") or ""
        if hello_b64:
            raw_pcm = base64_to_pcm16(hello_b64)
            speech_threshold = float(
                payload.get("speech_threshold", noise_floor + 200.0),
            )
            self._calibration_hello_pcm = trim_calibration_hello_audio(
                raw_pcm,
                speech_threshold=speech_threshold,
                noise_floor=noise_floor,
            )
            if len(self._calibration_hello_pcm) != len(raw_pcm):
                log.info(
                    "audio_bridge.calibration_hello_trimmed",
                    raw_bytes=len(raw_pcm),
                    trimmed_bytes=len(self._calibration_hello_pcm),
                )
        else:
            self._calibration_hello_pcm = None
        log.info(
            "audio_bridge.calibration_complete",
            noise_floor=noise_floor,
            user_speech_peak=user_speech_peak,
            vad_threshold=vad_settings.threshold,
            silence_ms=vad_settings.silence_ms,
            hello_audio_bytes=len(self._calibration_hello_pcm or b""),
        )

        self._calibration_phase = None
        self._awaiting_calibration = False

        if not self._loopback:
            self._set_conversation_state("connecting_openai")
            await self._ensure_realtime_connected(vad_settings=vad_settings)
            if self._calibration_hello_pcm:
                await self._begin_calibration_hello_conversation()
            else:
                log.warning("audio_bridge.calibration_missing_hello_audio")
                self._set_conversation_state("calibrating_retry")
                return False
        else:
            self._set_conversation_state("listening")
        return True

    async def _reject_calibration_hello_turn(self, transcript: str) -> None:
        """Invalid calibration speech — cancel the AI turn and ask for retry."""
        log.warning(
            "audio_bridge.calibration_hello_rejected",
            transcript=transcript[:80],
        )
        self._cancel_startup_response_timeout()
        self._awaiting_user_transcript = False
        self._buffered_transcripts.clear()
        if self._realtime_client is not None:
            if self._ai_speaking:
                await self._realtime_client.cancel_response()
            await self._realtime_client.clear_input_buffer()
        self._ai_speaking = False
        self._awaiting_first_calibration_playback = False
        self._calibration_hello_injected = False
        self._calibration_user_transcript_emitted = False
        self._ignore_audio_bytes_remaining = 0
        self._set_conversation_state("calibrating_retry")

    async def _begin_calibration_hello_conversation(self) -> None:
        """Use the calibration hello as the first user turn — no canned greeting."""
        if self._realtime_client is None or not self._session_ready:
            return
        if not self._calibration_hello_pcm:
            await self._begin_openai_conversation()
            return

        await self._realtime_client.clear_input_buffer()
        self._reset_opening_phase()
        self._arm_conversation()
        self._awaiting_user_transcript = True
        self._set_conversation_state("processing")
        if self._device_ready and not self._mic_muted:
            await self._send_mute(True)
        log.info(
            "audio_bridge.calibration_hello_received",
            bytes=len(self._calibration_hello_pcm),
            skipping_opening_greeting=True,
        )
        await self._inject_calibration_hello()
        self._calibration_hello_injected = True
        self._awaiting_first_calibration_playback = True
        self._schedule_startup_response_timeout()

    async def _inject_calibration_hello(self) -> None:
        """Append calibration speech to OpenAI and commit it as the first turn."""
        pcm = self._calibration_hello_pcm
        if not pcm or self._realtime_client is None:
            return

        offset = 0
        while offset < len(pcm):
            chunk = pcm[offset : offset + PLAY_AUDIO_CHUNK_BYTES]
            await self._realtime_client.send_audio(chunk)
            offset += len(chunk)
        await self._realtime_client.commit_input_buffer()
        log.info(
            "audio_bridge.calibration_hello_injected",
            bytes=len(pcm),
        )

    async def _begin_openai_conversation(self) -> None:
        """Unmute the device mic and have the assistant greet the user first."""
        if self._realtime_client is None or not self._session_ready:
            return

        await self._realtime_client.clear_input_buffer()
        self._opening_phase_active = True
        self._opening_nudge_sent = False
        self._awaiting_opening_greeting = True
        self._explicit_greeting_pending = True
        self._set_conversation_state("greeting")
        await self._send_mute(False)
        log.info("audio_bridge.unmuted_after_session_ready")
        await self._realtime_client.request_opening_greeting()
        log.info("audio_bridge.opening_greeting_started")

    def _schedule_opening_nudge(self) -> None:
        """Arm a single greeting repeat if the kid stays silent."""
        self._cancel_opening_nudge_task()

        async def _wait_and_nudge() -> None:
            try:
                await asyncio.sleep(OPENING_NUDGE_WAIT_SEC)
                await self._maybe_send_opening_nudge()
            except asyncio.CancelledError:
                raise

        self._opening_nudge_task = asyncio.create_task(
            _wait_and_nudge(),
            name="audio-bridge-opening-nudge",
        )
        log.info(
            "audio_bridge.opening_wait_started",
            timeout_sec=OPENING_NUDGE_WAIT_SEC,
        )

    async def _maybe_send_opening_nudge(self) -> None:
        """Repeat the opening greeting once after the wait timer expires."""
        if (
            not self._running
            or not self._opening_phase_active
            or self._conversation_armed
            or self._opening_nudge_sent
            or self._realtime_client is None
        ):
            return

        self._opening_nudge_sent = True
        self._explicit_greeting_pending = True
        self._awaiting_opening_greeting = True
        self._set_conversation_state("greeting")
        await self._realtime_client.clear_input_buffer()
        await self._realtime_client.request_opening_greeting()
        log.info("audio_bridge.opening_nudge_sent")

    async def _early_realtime_connect(self) -> None:
        """Open the Realtime WebSocket during device calibration."""
        from voice_assistant.openai_client.realtime import RealtimeClient

        if self._realtime_client is not None:
            return

        self._realtime_client = RealtimeClient(
            config=self._config,
        )
        try:
            await self._realtime_client.connect(send_session_update=False)
            self._event_task = asyncio.create_task(
                self._process_realtime_events(),
                name="audio-bridge-realtime-events",
            )
            log.info("audio_bridge.realtime_socket_connected")
        except Exception as exc:
            log.error("audio_bridge.realtime_connect_failed", error=str(exc))
            self._realtime_client = None
            self._set_conversation_state("connecting_openai_failed")
            raise

    async def _ensure_realtime_connected(self, *, vad_settings: Any) -> None:
        """Finish OpenAI setup with calibrated VAD and wait for session.updated."""
        if self._realtime_connect_task is not None:
            try:
                await self._realtime_connect_task
            except Exception:
                self._realtime_connect_task = None
                raise
            self._realtime_connect_task = None

        if self._realtime_client is None:
            from voice_assistant.openai_client.realtime import RealtimeClient

            self._realtime_client = RealtimeClient(
                config=self._config,
                vad_settings=vad_settings,
            )
            await self._realtime_client.connect()
            self._event_task = asyncio.create_task(
                self._process_realtime_events(),
                name="audio-bridge-realtime-events",
            )
            log.info("audio_bridge.realtime_connected")
        else:
            await self._realtime_client.update_vad_settings(vad_settings)

        self._session_ready = True
        log.info("audio_bridge.realtime_session_ready")

    def _cancel_realtime_connect_task(self) -> None:
        if self._realtime_connect_task is not None:
            self._realtime_connect_task.cancel()
            self._realtime_connect_task = None

    async def _connect_realtime(self, *, vad_settings: Any | None = None) -> None:
        """Create and connect a RealtimeClient."""
        from voice_assistant.openai_client.realtime import RealtimeClient

        self._realtime_client = RealtimeClient(
            config=self._config,
            vad_settings=vad_settings,
        )
        try:
            await self._realtime_client.connect()
            self._event_task = asyncio.create_task(
                self._process_realtime_events(),
                name="audio-bridge-realtime-events",
            )
            self._session_ready = True
            log.info("audio_bridge.realtime_connected")
        except Exception as exc:
            log.error("audio_bridge.realtime_connect_failed", error=str(exc))
            self._realtime_client = None
            raise

    async def _disconnect_realtime(self) -> None:
        """Disconnect the RealtimeClient if connected."""
        self._cancel_realtime_connect_task()
        self._session_ready = False
        if self._event_task is not None:
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
            self._event_task = None

        if self._realtime_client is not None:
            try:
                await self._realtime_client.disconnect()
            except Exception:
                pass
            self._realtime_client = None

    async def _send_mute(self, muted: bool) -> None:
        """Send MUTE_MIC or UNMUTE_MIC to the device."""
        if not self._device_ready:
            log.debug("audio_bridge.mute_skipped_not_ready", muted=muted)
            self._mic_muted = muted
            return
        msg_type = MessageType.MUTE_MIC if muted else MessageType.UNMUTE_MIC
        msg = create_message(msg_type)
        try:
            await self._transport.send_message(msg)
        except Exception as exc:
            log.warning("audio_bridge.mute_send_failed", muted=muted, error=str(exc))
        self._mic_muted = muted
        if self._mic_mute_callback is not None:
            try:
                self._mic_mute_callback(muted)
            except Exception:
                pass

    def _cancel_unmute_timeout(self) -> None:
        if self._unmute_timeout_task is not None:
            self._unmute_timeout_task.cancel()
            self._unmute_timeout_task = None

    def _schedule_unmute_timeout(self, duration_ms: int) -> None:
        """Safety fallback if PLAYBACK_COMPLETE is never received."""
        self._cancel_unmute_timeout()
        timeout_sec = (
            duration_ms + PLAYBACK_RECOVERY_MS + UNMUTE_SAFETY_MARGIN_MS
        ) / 1000.0

        async def _timeout() -> None:
            try:
                await asyncio.sleep(timeout_sec)
                if self._pending_playback_seq is not None and self._mic_muted:
                    log.warning(
                        "audio_bridge.unmute_timeout",
                        seq=self._pending_playback_seq,
                        timeout_sec=round(timeout_sec, 2),
                    )
                    await self.handle_playback_complete({
                        "sequence_number": self._pending_playback_seq,
                        "duration_ms": duration_ms,
                    })
            except asyncio.CancelledError:
                raise

        self._unmute_timeout_task = asyncio.create_task(
            _timeout(),
            name="audio-bridge-unmute-timeout",
        )

    async def _send_play_audio_chunk(self, pcm: bytes, *, is_final: bool) -> None:
        """Send one PLAY_AUDIO chunk to the device."""
        if not self._device_ready:
            log.debug("audio_bridge.play_audio_skipped_not_ready", bytes=len(pcm))
            return

        duration_ms = int(len(pcm) / BYTE_RATE * 1000)
        audio_b64 = pcm16_to_base64(pcm)
        play_msg = create_message(
            MessageType.PLAY_AUDIO,
            {
                "audio": audio_b64,
                "sequence_number": self._audio_seq,
                "is_final": is_final,
                "duration_ms": duration_ms,
            },
        )
        await self._transport.send_message(play_msg)
        self._chunks_sent_this_response += 1
        self._response_duration_ms += duration_ms
        log.info(
            "audio_bridge.play_audio_sent",
            seq=self._audio_seq,
            bytes=len(pcm),
            duration_ms=duration_ms,
            is_final=is_final,
        )

    async def _flush_partial_chunks(self) -> None:
        """Send full 4800-byte chunks while more data may still arrive."""
        while True:
            async with self._buffer_lock:
                if len(self._audio_buffer) < PLAY_AUDIO_CHUNK_BYTES:
                    return
                chunk = bytes(self._audio_buffer[:PLAY_AUDIO_CHUNK_BYTES])
                del self._audio_buffer[:PLAY_AUDIO_CHUNK_BYTES]
            await self._send_play_audio_chunk(chunk, is_final=False)

    async def _finalize_response_audio(self) -> None:
        """Flush remaining audio as the final chunk and arm unmute gating."""
        async with self._buffer_lock:
            remainder = bytes(self._audio_buffer)
            self._audio_buffer.clear()

        if remainder:
            await self._send_play_audio_chunk(remainder, is_final=True)
        elif self._chunks_sent_this_response > 0:
            await self._send_play_audio_chunk(b"", is_final=True)

        if self._chunks_sent_this_response > 0:
            self._pending_playback_seq = self._audio_seq
            if self._explicit_greeting_pending:
                self._pending_greeting_playback = True
            self._schedule_unmute_timeout(self._response_duration_ms)
            self._chunks_sent_this_response = 0
            self._response_duration_ms = 0

    async def _process_realtime_events(self) -> None:
        """Background task: process events from the RealtimeClient."""
        from voice_assistant.openai_client.realtime import (
            RealtimeAudioDelta,
            RealtimeErrorEvent,
            RealtimeResponseCreated,
            RealtimeResponseDone,
            RealtimeSpeechStarted,
            RealtimeSpeechStopped,
            RealtimeTranscript,
        )

        if self._realtime_client is None:
            return

        try:
            async for event in self._realtime_client.iter_events():
                if isinstance(event, RealtimeAudioDelta):
                    self._cancel_startup_response_timeout()
                    if not self._ai_speaking:
                        self._ai_speaking = True
                        self._set_conversation_state("ai_speaking")
                        self._audio_seq += 1
                        self._chunks_sent_this_response = 0
                        self._response_duration_ms = 0
                        await self._send_mute(True)

                    async with self._buffer_lock:
                        self._audio_buffer.extend(event.pcm_bytes)
                    await self._flush_partial_chunks()

                elif isinstance(event, RealtimeResponseCreated):
                    if self._calibration_hello_injected and not self._startup_response_started:
                        self._startup_response_started = True
                    if (
                        not self._explicit_greeting_pending
                        and not self._conversation_armed
                        and self._realtime_client is not None
                    ):
                        log.info("audio_bridge.phantom_response_cancelled")
                        await self._realtime_client.cancel_response()
                        await self._realtime_client.clear_input_buffer()

                elif isinstance(event, RealtimeResponseDone):
                    self._cancel_startup_response_timeout()
                    if self._awaiting_opening_greeting:
                        self._awaiting_opening_greeting = False
                        log.info("audio_bridge.opening_greeting_complete")
                    self._explicit_greeting_pending = False
                    await self._flush_partial_chunks()
                    await self._finalize_response_audio()

                elif isinstance(event, RealtimeSpeechStarted):
                    if self._should_ignore_live_speech_vad():
                        log.debug("audio_bridge.speech_started_ignored")
                        continue
                    self._set_conversation_state("user_speaking")
                    log.info("audio_bridge.user_speaking")

                elif isinstance(event, RealtimeSpeechStopped):
                    if self._should_ignore_live_speech_vad():
                        log.debug("audio_bridge.speech_stopped_ignored")
                        continue
                    self._flush_buffered_transcripts()
                    self._awaiting_user_transcript = True
                    self._set_conversation_state("processing")
                    log.info("audio_bridge.user_done_speaking")

                elif isinstance(event, RealtimeTranscript):
                    if event.role == "user" and event.final:
                        if likely_calibration_prompt_transcript(event.text):
                            log.info(
                                "audio_bridge.calibration_prompt_transcript_ignored",
                                text=event.text[:80],
                            )
                            continue
                        if self._calibration_hello_injected:
                            if not is_valid_calibration_hello_transcript(event.text):
                                await self._reject_calibration_hello_turn(event.text)
                                continue
                            if self._calibration_user_transcript_emitted:
                                log.debug(
                                    "audio_bridge.calibration_hello_duplicate_transcript_ignored",
                                    text=event.text[:80],
                                )
                                continue
                            self._calibration_user_transcript_emitted = True
                    if (
                        event.role == "user"
                        and event.final
                        and likely_echo_transcript(event.text, self._last_assistant_text)
                    ):
                        log.info(
                            "audio_bridge.echo_transcript_ignored",
                            text=event.text[:80],
                        )
                        continue
                    if (
                        event.role == "user"
                        and event.final
                        and is_meaningful_user_text(event.text)
                    ):
                        self._arm_conversation()
                    if event.role == "assistant" and event.final:
                        self._last_assistant_text = event.text
                    self._handle_transcript(event.role, event.text, event.final)

                elif isinstance(event, RealtimeErrorEvent):
                    self._cancel_startup_response_timeout()
                    log.error(
                        "audio_bridge.realtime_error",
                        message=event.message,
                        code=event.code,
                    )
                    if self._conversation_state == "processing":
                        self._awaiting_user_transcript = False
                        self._set_conversation_state("listening")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("audio_bridge.realtime_event_error", error=str(exc))
        finally:
            if self._audio_buffer or self._chunks_sent_this_response:
                await self._flush_partial_chunks()
                await self._finalize_response_audio()
