"""AudioBridge: relay audio frames between device and AI pipeline."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import structlog

from voice_assistant.audio.utils import (
    CALIBRATION_REPEAT_SEC,
    CALIBRATION_TIMEOUT_SEC,
    OPENING_NUDGE_WAIT_SEC,
    PLAY_AUDIO_CHUNK_BYTES,
    as_pcm_bytes,
    chunk_rms,
    compute_recovery_ms,
    generate_silence,
    is_meaningful_user_text,
    likely_calibration_prompt_transcript,
    likely_echo_transcript,
)
from voice_assistant.core.message import MessageType, create_message
from voice_assistant.transport.base import Transport

log = structlog.get_logger()

SAMPLE_RATE = 24000
BYTES_PER_SAMPLE = 2
BYTE_RATE = SAMPLE_RATE * BYTES_PER_SAMPLE
PLAYBACK_RECOVERY_MS = 300
UNMUTE_SAFETY_MARGIN_MS = 1000

# Brief silence appended to the end of every response so device-side playback
# clipping (e.g. aplay's ALSA buffer not fully draining before the pipe closes)
# trims trailing silence instead of the last word of the assistant's reply.
PLAYBACK_TAIL_SILENCE_MS = 300
TAIL_SILENCE = b"\x00" * (int(BYTE_RATE * PLAYBACK_TAIL_SILENCE_MS / 1000) & ~1)

# Assistant transcripts are held until the (async) user transcription lands so
# the log reads in conversational order. If that user transcript never arrives
# — empty/failed transcription, or it was filtered as echo/calibration noise —
# release the held assistant lines after this timeout so nothing is dropped.
USER_TRANSCRIPT_TIMEOUT_SEC = 5.0

# --- Barge-in (Phase 6a of the battery plan) -------------------------------
#
# The mic is muted while the assistant speaks, because the echo of the device's
# own speaker would otherwise be heard as the user talking. Phase 6a's way out
# is to listen only in the pauses of the assistant's own speech: the echo tail
# was measured at <50 ms, so once the speaker has actually gone quiet the mic is
# usable at full volume with no ducking, no echo canceller and no new hardware.
#
# The hard part is knowing when the speaker is *actually* quiet. Audio arrives
# from OpenAI far faster than real time, so a pause the app sees in the outgoing
# stream is not a pause happening now at the speaker, and the wire latency
# between them (SSH tunnel + aplay's own ALSA buffer) is both unknown and
# variable -- estimating it would spend the entire echo margin on guesswork.
#
# So the app does not estimate. It *creates* the boundary: it ends the segment
# at the pause with `is_final=True`, which makes the device drain playback and
# reply PLAYBACK_COMPLETE. That reply is an exact, latency-free "the speaker has
# stopped" signal, and the app already handles it. The listening window opens
# there. Nothing is playing during it, which is also why interrupting needs no
# stop-playback message: the app simply never sends the rest of the response.
BARGE_IN_SILENCE_RMS = 200.0
# 100 ms chunks. Four of them is a pause long enough to be a real phrase or
# sentence boundary rather than a stop consonant, which keeps the assistant from
# being chopped up mid-word.
BARGE_IN_MIN_GAP_CHUNKS = 4
# How long to hold the mic open at a boundary. Long enough for a child to start
# a word, short enough that the pause does not read as the assistant hanging.
BARGE_IN_WINDOW_MS = 700
# Floor on how often a response may be segmented. Without it a slow, pause-heavy
# response would be interrupted by listening windows every few hundred ms.
BARGE_IN_MIN_SEGMENT_MS = 1500

# --- Near-field voice level gate (Phase 7, step 1) -------------------------
#
# Goal: don't answer the parent across the room. The cheap signal the plan says
# to exhaust before reaching for a speaker-verification model is distance: the
# child is at the device, everyone else is further away, and §3 measured the
# child's speech at +10.2 dB over ambient at that position. The RMS is already
# being computed, so this costs nothing new.
#
# Levels are always logged; dropping frames is opt-in (`VOICE_LEVEL_GATE`) and
# off by default. That ordering is deliberate and is the plan's own: **a
# rejection that is never transmitted cannot be reviewed**, so the data needed to
# tune the threshold has to be gathered before the threshold is allowed to act.
#
# Threshold sits partway between the noise floor and the enrolled speech peak
# that calibration already measures on-device. Plain speech detection is lower
# than this on purpose -- a distant voice clears the noise floor easily but not
# the near-field level.
NEAR_FIELD_THRESHOLD_FRACTION = 0.5
# Once a frame is accepted, keep accepting for this long. Speech is not uniformly
# loud -- unstressed syllables and trailing words dip well below the threshold --
# and the plan is explicit that the failure asymmetry runs one way: a false
# reject means the child is ignored and the product feels broken, which is worse
# than answering the wrong person. Bias permissive.
NEAR_FIELD_HOLD_MS = 800

TranscriptCallback = Callable[[str, str, bool], None]
MicMuteCallback = Callable[[bool], None]
ConversationStateCallback = Callable[[str], None]
CalibrationTimeoutCallback = Callable[[], None]


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
        self._calibration_watchdog_task: asyncio.Task[None] | None = None
        self._calibration_timeout_callback: CalibrationTimeoutCallback | None = None

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
        self._user_transcript_timeout_task: asyncio.Task[None] | None = None
        self._opening_phase_active = False
        self._opening_nudge_sent = False
        self._explicit_greeting_pending = False
        self._pending_greeting_playback = False
        self._opening_nudge_task: asyncio.Task[None] | None = None
        self._realtime_connect_task: asyncio.Task[None] | None = None
        self._prewarm_connect_task: asyncio.Task[None] | None = None
        self._session_ready = False

        # Barge-in (Phase 6a). Loopback has no response to interrupt, so it is
        # excluded regardless of the flag.
        self._barge_in_enabled = bool(getattr(config, "barge_in", False)) and not loopback
        self._quiet_chunk_run = 0
        self._listening_for_barge_in = False
        self._awaiting_segment_playback = False
        self._barge_in_detected = False
        self._segment_ms_sent = 0
        self._finalize_deferred = False
        self._listen_window_task: asyncio.Task[None] | None = None

        # Near-field level gate (Phase 7, step 1). The threshold arrives with
        # the device's calibration; until then nothing is judged.
        self._level_gate_enabled = bool(getattr(config, "voice_level_gate", False))
        self._near_field_threshold: float | None = None
        self._near_field_hold_until = 0.0
        self._level_stats_accepted = 0
        self._level_stats_rejected = 0
        self._level_stats_peak = 0.0

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

    def set_calibration_timeout_callback(
        self,
        callback: CalibrationTimeoutCallback | None,
    ) -> None:
        self._calibration_timeout_callback = callback

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

    def _cancel_user_transcript_timeout(self) -> None:
        if self._user_transcript_timeout_task is not None:
            self._user_transcript_timeout_task.cancel()
            self._user_transcript_timeout_task = None

    def _schedule_user_transcript_timeout(self) -> None:
        """Release held assistant transcripts if the user transcript never lands."""
        self._cancel_user_transcript_timeout()

        async def _timeout() -> None:
            try:
                await asyncio.sleep(USER_TRANSCRIPT_TIMEOUT_SEC)
            except asyncio.CancelledError:
                raise
            if self._awaiting_user_transcript:
                log.info(
                    "audio_bridge.user_transcript_timeout_flush",
                    buffered=len(self._buffered_transcripts),
                )
                self._awaiting_user_transcript = False
                self._flush_buffered_transcripts()

        self._user_transcript_timeout_task = asyncio.create_task(
            _timeout(),
            name="audio-bridge-user-transcript-timeout",
        )

    def _resolve_awaited_user_turn(self) -> None:
        """A user turn produced no usable transcript; release held lines anyway."""
        if not self._awaiting_user_transcript:
            return
        self._cancel_user_transcript_timeout()
        self._awaiting_user_transcript = False
        self._flush_buffered_transcripts()

    def _handle_transcript(self, role: str, text: str, final: bool) -> None:
        """Emit transcripts in conversational order (user before assistant).

        OpenAI often delivers the assistant transcript before async user
        transcription completes; buffer assistant lines until the user turn lands.
        A timeout (see _schedule_user_transcript_timeout) guarantees held lines
        are still emitted if that user transcript never arrives.
        """
        if role == "assistant" and self._awaiting_user_transcript:
            self._buffered_transcripts.append((role, text, final))
            return

        if role == "user" and final:
            self._cancel_user_transcript_timeout()
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

    def _cancel_calibration_watchdog(self) -> None:
        if self._calibration_watchdog_task is not None:
            self._calibration_watchdog_task.cancel()
            self._calibration_watchdog_task = None

    def _schedule_calibration_watchdog(self) -> None:
        """Re-prompt for the calibration hello every CALIBRATION_REPEAT_SEC;
        give up and signal a timeout after CALIBRATION_TIMEOUT_SEC of silence."""
        self._cancel_calibration_watchdog()

        async def _watch() -> None:
            elapsed = 0.0
            try:
                while self._awaiting_calibration:
                    await asyncio.sleep(CALIBRATION_REPEAT_SEC)
                    if not self._awaiting_calibration:
                        return
                    elapsed += CALIBRATION_REPEAT_SEC
                    if elapsed >= CALIBRATION_TIMEOUT_SEC:
                        log.warning(
                            "audio_bridge.calibration_timeout",
                            elapsed_sec=elapsed,
                        )
                        if self._calibration_timeout_callback is not None:
                            self._calibration_timeout_callback()
                        return
                    log.info(
                        "audio_bridge.calibration_prompt_repeat",
                        elapsed_sec=elapsed,
                    )
                    self._set_conversation_state("calibrating_retry")
            except asyncio.CancelledError:
                raise

        self._calibration_watchdog_task = asyncio.create_task(
            _watch(),
            name="audio-bridge-calibration-watchdog",
        )

    def _arm_conversation(self) -> None:
        if self._conversation_armed:
            return
        self._conversation_armed = True
        self._opening_phase_active = False
        self._cancel_opening_nudge_task()
        log.info("audio_bridge.conversation_armed")

    def _reset_transcript_ordering(self) -> None:
        # Emit any still-held assistant lines before clearing so a teardown mid
        # turn never silently drops them.
        self._cancel_user_transcript_timeout()
        self._flush_buffered_transcripts()
        self._awaiting_user_transcript = False

    def _set_conversation_state(self, state: str) -> None:
        self._conversation_state = state
        if self._conversation_state_callback is not None:
            try:
                self._conversation_state_callback(state)
            except Exception:
                pass

    def _should_ignore_live_speech_vad(self) -> bool:
        """Ignore server VAD while the AI speaks or greets — not for the live mic."""
        if self._listening_for_barge_in:
            # A barge-in window (Phase 6a): the segment has drained, so the
            # speaker is quiet and VAD firing here is the user, not the echo.
            # This has to come before the _ai_speaking check below, which is
            # otherwise still true for the response we are in the middle of.
            return False
        if self._ai_speaking:
            return True
        if time.monotonic() < self._recovery_until:
            return True
        if self._awaiting_opening_greeting:
            return True
        return False

    def set_device_ready(self, ready: bool) -> None:
        """Gate device commands until HELLO → HELLO_ACK completes."""
        self._device_ready = ready

    def adopt_prewarmed_realtime(
        self,
        client: Any,
        connect_task: asyncio.Task[None] | None,
    ) -> None:
        """Take over a Realtime socket the session opened at HELLO_ACK.

        _early_realtime_connect finishes this handshake instead of opening a
        second socket, so the cold TLS/WS cost is already (mostly) paid by the
        time calibration completes. Falls back to a fresh connect if the
        pre-warmed socket went stale while the dashboard waited to Start.
        """
        self._realtime_client = client
        self._prewarm_connect_task = connect_task

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
            self._schedule_calibration_watchdog()
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
        self._cancel_calibration_watchdog()
        log.info(
            "audio_bridge.stopped",
            frames_processed=self._frame_count,
        )

    async def stop_async(self) -> None:
        """Stop the bridge and disconnect from OpenAI if connected."""
        self._cancel_unmute_timeout()
        self._cancel_opening_nudge_task()
        self._cancel_calibration_watchdog()
        self._reset_barge_in_state()
        if self._mic_muted and self._device_ready:
            await self._send_mute(False)
        self.stop()
        await self._disconnect_realtime()

    def _reset_barge_in_state(self) -> None:
        """Drop any in-flight barge-in boundary (Phase 6a).

        A listening window left running across a stop or a device disconnect
        would send MUTE_MIC to a device that is gone, and would resume flushing a
        response nobody is listening to.
        """
        self._cancel_listen_window()
        self._listening_for_barge_in = False
        self._awaiting_segment_playback = False
        self._barge_in_detected = False
        self._finalize_deferred = False
        self._quiet_chunk_run = 0
        self._segment_ms_sent = 0

    def _reset_level_gate_state(self) -> None:
        """Forget the near-field threshold (Phase 7).

        It is calibrated per session against this room and this speaker; carrying
        it into a fresh session -- possibly a different room -- would gate on a
        measurement that no longer describes anything.
        """
        self._log_level_gate_summary()
        self._near_field_threshold = None
        self._near_field_hold_until = 0.0

    async def reset_on_disconnect(self) -> None:
        """Tear down playback/OpenAI state without sending device commands."""
        self._cancel_unmute_timeout()
        self._reset_barge_in_state()
        self._reset_level_gate_state()
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
        self._cancel_calibration_watchdog()
        async with self._buffer_lock:
            self._audio_buffer.clear()
        await self._disconnect_realtime()
        self._set_conversation_state("idle")
        self._awaiting_calibration = False
        self._calibration_phase = None
        self._awaiting_opening_greeting = False
        self._running = False
        self._reset_transcript_ordering()
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
            pcm_bytes = as_pcm_bytes(payload.get("audio"))
            if pcm_bytes:
                near_field = self._classify_near_field(pcm_bytes)
                if near_field is False and self._level_gate_enabled:
                    # Too quiet to be the child at the device. Dropped rather
                    # than forwarded -- but only because the gate was explicitly
                    # turned on; the classification happens either way so the
                    # logs can be reviewed before it is.
                    log.debug(
                        "audio_bridge.frame_gated_far_field",
                        seq=payload.get("sequence_number"),
                    )
                    return
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

    def _classify_near_field(self, pcm_bytes: bytes) -> bool | None:
        """Is this frame loud enough to be the child at the device?

        Returns None while there is nothing to judge against (before
        calibration), which callers must treat as "don't gate" -- an unknown
        threshold is not evidence of a far-field speaker.

        Counts every verdict for the per-turn summary regardless of whether the
        gate is enabled: gathering that record *is* step 1 of Phase 7.
        """
        if self._near_field_threshold is None:
            return None

        level = chunk_rms(pcm_bytes, stride=4)
        self._level_stats_peak = max(self._level_stats_peak, level)

        now = time.monotonic()
        if level >= self._near_field_threshold:
            self._near_field_hold_until = now + NEAR_FIELD_HOLD_MS / 1000.0
            self._level_stats_accepted += 1
            return True
        if now < self._near_field_hold_until:
            # Mid-utterance dip, not a new speaker. Held open deliberately.
            self._level_stats_accepted += 1
            return True

        self._level_stats_rejected += 1
        return False

    def _log_level_gate_summary(self) -> None:
        """Emit one reviewable line per user turn, then reset the counters.

        Per-frame lines are debug-only -- a 15 s turn is ~150 frames, which is
        not something anyone will read. This summary is the artifact Phase 7's
        "evaluate from real logs" step is meant to consume, which is why it is
        logged at info level even when the gate is doing nothing.
        """
        total = self._level_stats_accepted + self._level_stats_rejected
        if total == 0:
            return
        log.info(
            "audio_bridge.voice_level_summary",
            near_field_frames=self._level_stats_accepted,
            far_field_frames=self._level_stats_rejected,
            peak_rms=round(self._level_stats_peak, 1),
            threshold=round(self._near_field_threshold or 0.0, 1),
            gate_enforcing=self._level_gate_enabled,
        )
        self._level_stats_accepted = 0
        self._level_stats_rejected = 0
        self._level_stats_peak = 0.0

    async def handle_audio_gap(self, payload: dict) -> None:
        """Synthesize the silence a device elided, so OpenAI's timeline stays intact.

        The device only sends this once it has decided a chunk was silence
        (Phase 5b); the app's job is purely to make that invisible downstream
        by feeding OpenAI the same silence it would have received frame by
        frame. Loopback mode doesn't need timeline fidelity for anything, so
        it's a no-op there.
        """
        if not self._running or self._awaiting_calibration or self._awaiting_opening_greeting:
            return
        if self._loopback:
            return

        duration_ms = payload.get("duration_ms", 0)
        if duration_ms <= 0:
            return

        if self._realtime_client is not None and self._realtime_client.is_connected:
            silence = generate_silence(duration_ms)
            log.debug(
                "audio_bridge.synthesizing_gap",
                duration_ms=duration_ms,
                bytes=len(silence),
                seq=payload.get("sequence_number"),
            )
            await self._realtime_client.send_audio(silence)

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

        if self._awaiting_segment_playback:
            # Not the end of the reply -- the end of a segment we deliberately
            # cut at a pause. The device has drained, so the speaker is now
            # genuinely quiet and the mic can be trusted for a moment.
            self._cancel_listen_window()
            self._listen_window_task = asyncio.create_task(
                self._open_barge_in_window(),
                name="audio-bridge-barge-in-window",
            )
            return

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
        log.info(
            "audio_bridge.calibration_complete",
            noise_floor=noise_floor,
            user_speech_peak=user_speech_peak,
            vad_eagerness=vad_settings.eagerness,
            vad_threshold=vad_settings.threshold,
            silence_ms=vad_settings.silence_ms,
        )

        # Near-field threshold (Phase 7): partway between the ambient floor and
        # the level the child actually spoke at during calibration.
        self._near_field_threshold = (
            noise_floor + voice_margin * NEAR_FIELD_THRESHOLD_FRACTION
        )
        log.info(
            "audio_bridge.near_field_threshold_set",
            threshold=round(self._near_field_threshold, 1),
            noise_floor=noise_floor,
            user_speech_peak=user_speech_peak,
            gate_enforcing=self._level_gate_enabled,
        )

        self._calibration_phase = None
        self._awaiting_calibration = False
        self._cancel_calibration_watchdog()

        if not self._loopback:
            # Greet the child first, then wait for them to speak. We do NOT
            # inject the calibration audio as a fabricated user turn -- that
            # made the assistant "respond to a hello you never said" and, by
            # arming the conversation immediately, let the AI's own echo
            # trigger an endless self-conversation.
            self._set_conversation_state("connecting_openai")
            await self._ensure_realtime_connected(vad_settings=vad_settings)
            await self._begin_openai_conversation()
        else:
            self._set_conversation_state("listening")
            # The device only starts streaming AUDIO_FRAMEs once it sees
            # UNMUTE_MIC (or a skip_calibration resume) -- see
            # zero2w_client.py/pi5_client.py's `_stream_to_laptop` gate. The
            # non-loopback path sends this implicitly once the opening
            # greeting finishes playing; loopback has no greeting, so without
            # this the device stays muted forever after a fresh calibration.
            await self._send_mute(False)
        return True

    async def _begin_openai_conversation(self) -> None:
        """Have the assistant greet the user first, mic muted so it can't self-echo.

        The device stays muted for the whole greeting; handle_playback_complete
        unmutes once the greeting has finished draining through the speaker,
        which then transitions to waiting_for_kid.
        """
        if self._realtime_client is None or not self._session_ready:
            return

        await self._realtime_client.clear_input_buffer()
        self._opening_phase_active = True
        self._opening_nudge_sent = False
        self._awaiting_opening_greeting = True
        self._explicit_greeting_pending = True
        self._set_conversation_state("greeting")
        await self._send_mute(True)
        log.info("audio_bridge.muted_for_opening_greeting")
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
        """Open (or adopt) the Realtime WebSocket during device calibration."""
        from voice_assistant.openai_client.realtime import RealtimeClient

        if self._realtime_client is not None:
            # A socket pre-warmed by the session at HELLO_ACK: finish its
            # handshake and wire up event processing without a second connect.
            try:
                if self._prewarm_connect_task is not None:
                    await self._prewarm_connect_task
                    self._prewarm_connect_task = None
                if self._realtime_client.is_connected:
                    if self._event_task is None:
                        self._event_task = asyncio.create_task(
                            self._process_realtime_events(),
                            name="audio-bridge-realtime-events",
                        )
                    log.info(
                        "audio_bridge.realtime_socket_connected", prewarmed=True
                    )
                    return
                # Pre-warmed socket went stale; fall through to a fresh connect.
                log.info("audio_bridge.realtime_prewarm_stale")
            except Exception as exc:
                log.warning("audio_bridge.realtime_prewarm_failed", error=str(exc))
            self._prewarm_connect_task = None
            self._realtime_client = None

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
        # Raw PCM: the transport base64s it only if this connection is on the
        # JSON path, so a binary-framed device never pays for the encode.
        play_msg = create_message(
            MessageType.PLAY_AUDIO,
            {
                "audio": pcm,
                "sequence_number": self._audio_seq,
                "is_final": is_final,
                "duration_ms": duration_ms,
            },
        )
        await self._transport.send_message(play_msg)
        self._chunks_sent_this_response += 1
        self._response_duration_ms += duration_ms
        self._segment_ms_sent += duration_ms
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
            if self._listening_for_barge_in or self._awaiting_segment_playback:
                # A barge-in boundary is in flight: the device is either
                # draining the segment we just ended or has the mic open in the
                # pause after it. Either way the rest of the response waits --
                # sending it now would refill the speaker and close the window.
                return
            async with self._buffer_lock:
                if len(self._audio_buffer) < PLAY_AUDIO_CHUNK_BYTES:
                    return
                chunk = bytes(self._audio_buffer[:PLAY_AUDIO_CHUNK_BYTES])
                del self._audio_buffer[:PLAY_AUDIO_CHUNK_BYTES]
            if self._is_barge_in_boundary(chunk):
                await self._end_segment_for_barge_in(chunk)
                return
            await self._send_play_audio_chunk(chunk, is_final=False)

    def _is_barge_in_boundary(self, chunk: bytes) -> bool:
        """Is this chunk the end of a pause long enough to listen in?

        Counts consecutive quiet chunks and reports the boundary once the run is
        long enough *and* enough of the response has played since the last one.
        Called for every outgoing chunk, so it stays an RMS with stride 4 -- the
        same cheap approximation Phase 5b's device-side gate uses.
        """
        if not self._barge_in_enabled:
            return False

        if chunk_rms(chunk, stride=4) >= BARGE_IN_SILENCE_RMS:
            self._quiet_chunk_run = 0
            return False

        self._quiet_chunk_run += 1
        if self._quiet_chunk_run < BARGE_IN_MIN_GAP_CHUNKS:
            return False

        # Measured in audio *delivered*, not wall clock. Audio arrives from
        # OpenAI far faster than real time, so a wall-clock floor here would be
        # ~0 ms for the whole response and never suppress anything -- the same
        # confusion between send time and playback time this design exists to
        # avoid.
        if self._segment_ms_sent < BARGE_IN_MIN_SEGMENT_MS:
            return False

        return True

    async def _end_segment_for_barge_in(self, chunk: bytes) -> None:
        """Close the current segment at a pause so the device drains and replies.

        The reply (PLAYBACK_COMPLETE for this sequence number) is what opens the
        listening window -- see handle_playback_complete.
        """
        self._quiet_chunk_run = 0
        self._segment_ms_sent = 0
        self._awaiting_segment_playback = True
        self._pending_playback_seq = self._audio_seq
        await self._send_play_audio_chunk(chunk, is_final=True)
        log.info(
            "audio_bridge.barge_in_segment_ended",
            seq=self._audio_seq,
            buffered_bytes=len(self._audio_buffer),
        )

    async def _open_barge_in_window(self) -> None:
        """Unmute at a drained segment boundary, listen, then carry on.

        Incoming frames during the window reach OpenAI through the ordinary
        handle_audio_frame path; its own turn detection (semantic_vad with
        interrupt_response) is what decides whether they were an interruption.
        This method only decides *when* the mic is trustworthy.
        """
        self._listening_for_barge_in = True
        self._awaiting_segment_playback = False
        await self._send_mute(False)
        log.info("audio_bridge.barge_in_window_open", window_ms=BARGE_IN_WINDOW_MS)

        try:
            await asyncio.sleep(BARGE_IN_WINDOW_MS / 1000.0)
        except asyncio.CancelledError:
            # Cancelled means a barge-in landed; _handle_barge_in owns the
            # cleanup, so leave the mic open for the user who is now talking.
            raise

        self._listening_for_barge_in = False
        if self._barge_in_detected:
            return

        await self._send_mute(True)
        # Next segment gets its own sequence number so its PLAYBACK_COMPLETE
        # can't be confused with the one we just consumed.
        self._audio_seq += 1
        log.info("audio_bridge.barge_in_window_closed", resumed_seq=self._audio_seq)
        await self._flush_partial_chunks()
        if self._finalize_deferred:
            # The reply finished generating during the window; its held tail is
            # flushed now that the speaker is ours again.
            self._finalize_deferred = False
            await self._finalize_response_audio()

    def _cancel_listen_window(self) -> None:
        if self._listen_window_task is not None:
            self._listen_window_task.cancel()
            self._listen_window_task = None

    async def _handle_barge_in(self) -> None:
        """The user spoke during a listening window: abandon the rest of the reply.

        Nothing is playing (the segment drained before the window opened), so
        there is no playback to stop -- the interruption is simply the app never
        sending the audio it was holding. OpenAI's own interrupt_response stops
        it generating more.
        """
        self._barge_in_detected = True
        self._listening_for_barge_in = False
        self._awaiting_segment_playback = False
        # The interrupted reply's held tail is abandoned, not deferred.
        self._finalize_deferred = False
        self._cancel_listen_window()

        async with self._buffer_lock:
            discarded = len(self._audio_buffer)
            self._audio_buffer.clear()

        self._cancel_unmute_timeout()
        self._pending_playback_seq = None
        self._ai_speaking = False
        self._mic_muted = False
        self._chunks_sent_this_response = 0
        self._response_duration_ms = 0
        log.info("audio_bridge.barge_in_detected", discarded_bytes=discarded)

    async def _finalize_response_audio(self) -> None:
        """Flush remaining audio (plus a trailing silence pad) and arm unmute gating."""
        if self._listening_for_barge_in or self._awaiting_segment_playback:
            # The reply finished generating while a barge-in boundary was in
            # flight. Finalizing now would push the held tail into a speaker the
            # window depends on staying quiet; the window's own close path
            # finalizes instead, once it has resumed and drained the remainder.
            self._finalize_deferred = True
            log.debug("audio_bridge.finalize_deferred_for_barge_in")
            return

        async with self._buffer_lock:
            has_audio = self._chunks_sent_this_response > 0 or len(self._audio_buffer) > 0
            if has_audio and TAIL_SILENCE:
                self._audio_buffer.extend(TAIL_SILENCE)

        # The silence pad may push the buffer past one or more full chunks.
        await self._flush_partial_chunks()

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
                    if self._barge_in_detected:
                        # Interrupted reply still draining out of OpenAI before
                        # its cancel lands. Discard these -- buffering them
                        # would re-mute the mic and talk over the user.
                        continue
                    if not self._ai_speaking:
                        self._ai_speaking = True
                        self._set_conversation_state("ai_speaking")
                        self._audio_seq += 1
                        self._chunks_sent_this_response = 0
                        self._response_duration_ms = 0
                        self._quiet_chunk_run = 0
                        self._segment_ms_sent = 0
                        await self._send_mute(True)

                    async with self._buffer_lock:
                        self._audio_buffer.extend(event.pcm_bytes)
                    await self._flush_partial_chunks()

                elif isinstance(event, RealtimeResponseCreated):
                    if (
                        not self._explicit_greeting_pending
                        and not self._conversation_armed
                        and self._realtime_client is not None
                    ):
                        # Nothing armed this turn (no greeting, user hasn't
                        # spoken) -- a response created here is server VAD
                        # firing on the AI's own echo or room noise. Cancel it
                        # so the assistant never talks to itself.
                        log.info("audio_bridge.phantom_response_cancelled")
                        await self._realtime_client.cancel_response()
                        await self._realtime_client.clear_input_buffer()

                elif isinstance(event, RealtimeResponseDone):
                    if self._awaiting_opening_greeting:
                        self._awaiting_opening_greeting = False
                        log.info("audio_bridge.opening_greeting_complete")
                    if self._barge_in_detected:
                        # The reply the user interrupted. _handle_barge_in
                        # already dropped its audio and released the mic; there
                        # is nothing to flush or finalize. Clear the flag so the
                        # reply to what they just said is handled normally.
                        self._barge_in_detected = False
                        log.info("audio_bridge.barge_in_response_closed")
                        continue
                    await self._flush_partial_chunks()
                    # Finalize while _explicit_greeting_pending is still set so
                    # the greeting's playback arms the waiting_for_kid nudge.
                    await self._finalize_response_audio()
                    self._explicit_greeting_pending = False

                elif isinstance(event, RealtimeSpeechStarted):
                    if self._should_ignore_live_speech_vad():
                        log.debug("audio_bridge.speech_started_ignored")
                        continue
                    if self._listening_for_barge_in:
                        # Phase 6a: the user started talking in a pause of the
                        # assistant's own reply. Drop the rest of that reply
                        # before arming the turn below, so the two do not
                        # overlap.
                        await self._handle_barge_in()
                    # A genuine user turn (past greeting/AI-speech/recovery):
                    # arm now, before response.created, so the phantom guard
                    # above doesn't cancel the child's first real response.
                    self._arm_conversation()
                    self._set_conversation_state("user_speaking")
                    log.info("audio_bridge.user_speaking")

                elif isinstance(event, RealtimeSpeechStopped):
                    if self._should_ignore_live_speech_vad():
                        log.debug("audio_bridge.speech_stopped_ignored")
                        continue
                    self._log_level_gate_summary()
                    self._flush_buffered_transcripts()
                    self._awaiting_user_transcript = True
                    self._schedule_user_transcript_timeout()
                    self._set_conversation_state("processing")
                    log.info("audio_bridge.user_done_speaking")

                elif isinstance(event, RealtimeTranscript):
                    if (
                        event.role == "user"
                        and event.final
                        and likely_calibration_prompt_transcript(event.text)
                    ):
                        # Stray "say hello to start" prompt picked up by the mic.
                        log.info(
                            "audio_bridge.calibration_prompt_transcript_ignored",
                            text=event.text[:80],
                        )
                        # This was the user turn we were holding lines for; release
                        # the buffered assistant reply so it is never orphaned.
                        self._resolve_awaited_user_turn()
                        continue
                    if (
                        event.role == "user"
                        and event.final
                        and likely_echo_transcript(event.text, self._last_assistant_text)
                    ):
                        log.info(
                            "audio_bridge.echo_transcript_ignored",
                            text=event.text[:80],
                        )
                        self._resolve_awaited_user_turn()
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
                    log.error(
                        "audio_bridge.realtime_error",
                        message=event.message,
                        code=event.code,
                    )
                    # Release any assistant lines held for the in-flight turn so
                    # an API error never strands them.
                    self._resolve_awaited_user_turn()
                    if self._conversation_state == "processing":
                        self._set_conversation_state("listening")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("audio_bridge.realtime_event_error", error=str(exc))
        finally:
            self._cancel_user_transcript_timeout()
            self._flush_buffered_transcripts()
            if self._audio_buffer or self._chunks_sent_this_response:
                await self._flush_partial_chunks()
                await self._finalize_response_audio()
