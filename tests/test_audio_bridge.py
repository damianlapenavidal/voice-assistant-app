"""Tests for AudioBridge loopback relay and OpenAI whole-chunk playback."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from voice_assistant.audio.bridge import AudioBridge, TAIL_SILENCE
from voice_assistant.audio.utils import base64_to_pcm16, pcm16_to_base64
from voice_assistant.core.message import MessageType
from voice_assistant.core.session import SessionManager, SessionState
from voice_assistant.openai_client.realtime import (
    RealtimeAudioDelta,
    RealtimeResponseCreated,
    RealtimeResponseDone,
    RealtimeSpeechStarted,
    RealtimeSpeechStopped,
    RealtimeTranscript,
)
from voice_assistant.transport.base import Transport
from voice_assistant.transport.mock_transport import MockTransport


def _make_mock_transport() -> Transport:
    t = AsyncMock(spec=Transport)
    t.is_connected = True
    return t


def _frame_payload(seq: int = 1, audio: str = "dGVzdA==") -> dict:
    return {"audio": audio, "sequence_number": seq, "timestamp": "2025-01-01T00:00:00Z"}


class TestAudioBridgeLifecycle:
    def test_not_running_by_default(self) -> None:
        bridge = AudioBridge(_make_mock_transport())
        assert not bridge.is_running

    def test_start_sets_running(self) -> None:
        bridge = AudioBridge(_make_mock_transport())
        bridge.start()
        assert bridge.is_running

    def test_stop_clears_running(self) -> None:
        bridge = AudioBridge(_make_mock_transport())
        bridge.start()
        bridge.stop()
        assert not bridge.is_running

    def test_frame_count_resets_on_start(self) -> None:
        bridge = AudioBridge(_make_mock_transport())
        bridge.start()
        bridge._frame_count = 5
        bridge.start()
        assert bridge.frame_count == 0


class TestLoopbackRelay:
    async def test_loopback_sends_play_audio(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=True)
        bridge.start()

        await bridge.handle_audio_frame(_frame_payload(seq=1))

        transport.send_message.assert_called_once()
        sent_msg = transport.send_message.call_args[0][0]
        assert sent_msg.type == MessageType.PLAY_AUDIO
        assert sent_msg.payload["audio"] == "dGVzdA=="
        assert sent_msg.payload["sequence_number"] == 1

    async def test_loopback_preserves_audio_data(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=True)
        bridge.start()

        audio = "YWJjZGVmZw=="
        await bridge.handle_audio_frame(_frame_payload(seq=42, audio=audio))

        sent_msg = transport.send_message.call_args[0][0]
        assert sent_msg.payload["audio"] == audio
        assert sent_msg.payload["sequence_number"] == 42

    async def test_calibration_complete_sends_unmute_so_device_starts_streaming(
        self,
    ) -> None:
        """The device (pi5_client.py/zero2w_client.py) only flips its
        `_stream_to_laptop` gate on `skip_calibration` resume or on receiving
        UNMUTE_MIC. Loopback has no opening-greeting mute/unmute cycle, so
        without an explicit UNMUTE_MIC here the device stays silent forever
        after a fresh (non-resumed) calibration.
        """
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=True)
        bridge.set_device_ready(True)
        bridge.start()

        await bridge.handle_calibration_complete({
            "noise_floor": 350.0,
            "user_speech_peak": 850.0,
        })

        unmute_calls = [
            c for c in transport.send_message.call_args_list
            if c[0][0].type == MessageType.UNMUTE_MIC
        ]
        assert len(unmute_calls) == 1

    async def test_multiple_frames_increment_count(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=True)
        bridge.start()

        for i in range(5):
            await bridge.handle_audio_frame(_frame_payload(seq=i))

        assert bridge.frame_count == 5
        assert transport.send_message.call_count == 5

    async def test_does_nothing_when_not_running(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=True)

        await bridge.handle_audio_frame(_frame_payload())

        transport.send_message.assert_not_called()
        assert bridge.frame_count == 0

    async def test_loopback_false_does_not_send(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.start()

        await bridge.handle_audio_frame(_frame_payload())

        transport.send_message.assert_not_called()
        assert bridge.frame_count == 1


    async def test_openai_mode_starts_early_connect_and_greets_first(self) -> None:
        from unittest.mock import patch

        import voice_assistant.openai_client.realtime as rt_mod

        transport = _make_mock_transport()
        mock_instance = AsyncMock()
        mock_instance.is_connected = True

        async def fake_iter():
            return
            yield

        mock_instance.iter_events = fake_iter
        mock_instance.connect = AsyncMock()
        mock_instance.update_vad_settings = AsyncMock()

        bridge = AudioBridge(transport, loopback=False, config=None)
        bridge.set_device_ready(True)
        with patch.object(rt_mod, "RealtimeClient", return_value=mock_instance):
            await bridge.start_async()

            assert bridge.conversation_state == "calibrating"
            await asyncio.sleep(0)
            mock_instance.connect.assert_called_once_with(send_session_update=False)
            # Audio captured during calibration is not forwarded to OpenAI.
            await bridge.handle_audio_frame(_frame_payload())
            transport.send_message.assert_not_called()

            await bridge.handle_calibration_complete({
                "noise_floor": 350.0,
                "user_speech_peak": 850.0,
            })
            # Calibration confirmed a real voice -> the assistant greets first.
            assert bridge.conversation_state == "greeting"
            mock_instance.update_vad_settings.assert_called_once()
            mock_instance.request_opening_greeting.assert_called_once()

    async def test_calibration_greets_first_without_injecting_a_turn(self) -> None:
        from unittest.mock import patch

        import voice_assistant.openai_client.realtime as rt_mod

        transport = _make_mock_transport()
        mock_instance = AsyncMock()
        mock_instance.is_connected = True

        async def fake_iter():
            return
            yield

        mock_instance.iter_events = fake_iter
        mock_instance.connect = AsyncMock()
        mock_instance.update_vad_settings = AsyncMock()

        bridge = AudioBridge(transport, loopback=False, config=None)
        bridge.set_device_ready(True)
        with patch.object(rt_mod, "RealtimeClient", return_value=mock_instance):
            await bridge.start_async()
            await asyncio.sleep(0)

            await bridge.handle_calibration_complete({
                "noise_floor": 350.0,
                "user_speech_peak": 850.0,
            })

            # Assistant greets; no fabricated user turn is committed, and the
            # conversation stays un-armed until the child actually speaks.
            assert bridge.conversation_state == "greeting"
            mock_instance.request_opening_greeting.assert_called_once()
            mock_instance.commit_input_buffer.assert_not_called()
            assert not bridge._conversation_armed

            # Mic is muted for the greeting so it cannot self-echo.
            mute_calls = [
                c for c in transport.send_message.call_args_list
                if c[0][0].type == MessageType.MUTE_MIC
            ]
            assert len(mute_calls) >= 1
            unmute_calls = [
                c for c in transport.send_message.call_args_list
                if c[0][0].type == MessageType.UNMUTE_MIC
            ]
            assert len(unmute_calls) == 0

    async def test_calibration_greets_even_without_hello_audio(self) -> None:
        from unittest.mock import patch

        import voice_assistant.openai_client.realtime as rt_mod

        transport = _make_mock_transport()
        mock_instance = AsyncMock()
        mock_instance.is_connected = True

        async def fake_iter():
            return
            yield

        mock_instance.iter_events = fake_iter
        mock_instance.connect = AsyncMock()
        mock_instance.update_vad_settings = AsyncMock()

        bridge = AudioBridge(transport, loopback=False, config=None)
        with patch.object(rt_mod, "RealtimeClient", return_value=mock_instance):
            await bridge.start_async()
            await asyncio.sleep(0)
            await bridge.handle_calibration_complete({
                "noise_floor": 350.0,
                "user_speech_peak": 850.0,
            })

        assert bridge.conversation_state == "greeting"
        mock_instance.request_opening_greeting.assert_called_once()

    async def test_calibration_rejected_when_no_speech_detected(self) -> None:
        from unittest.mock import patch

        import voice_assistant.openai_client.realtime as rt_mod

        transport = _make_mock_transport()
        mock_instance = AsyncMock()
        mock_instance.is_connected = True

        async def fake_iter():
            return
            yield

        mock_instance.iter_events = fake_iter
        mock_instance.connect = AsyncMock()

        bridge = AudioBridge(transport, loopback=False, config=None)
        with patch.object(rt_mod, "RealtimeClient", return_value=mock_instance):
            await bridge.start_async()
            await asyncio.sleep(0)
            calibrated = await bridge.handle_calibration_complete({
                "noise_floor": 350.0,
                "user_speech_peak": 850.0,
                "speech_detected": False,
            })

        assert calibrated is False
        assert bridge.conversation_state == "calibrating_retry"
        mock_instance.request_opening_greeting.assert_not_called()

    async def test_calibration_prompt_transcript_ignored(self) -> None:
        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        transcripts: list[tuple[str, str, bool]] = []
        bridge.set_transcript_callback(lambda r, t, f: transcripts.append((r, t, f)))

        await TestOpeningListenGuard()._run_event_queue(
            bridge,
            [RealtimeTranscript(role="user", text="Say hello to start.", final=True)],
        )

        assert transcripts == []


class TestAudioBridgeResume:
    async def test_resume_async_skips_calibration_and_unmutes(self) -> None:
        from unittest.mock import patch

        import voice_assistant.openai_client.realtime as rt_mod

        transport = _make_mock_transport()
        mock_instance = AsyncMock()
        mock_instance.is_connected = True

        async def fake_iter():
            await asyncio.Event().wait()
            return
            yield

        mock_instance.iter_events = fake_iter
        mock_instance.connect = AsyncMock()
        mock_instance.update_vad_settings = AsyncMock()
        mock_instance.clear_input_buffer = AsyncMock()

        bridge = AudioBridge(transport, loopback=False, config=None)
        bridge.set_device_ready(True)
        with patch.object(rt_mod, "RealtimeClient", return_value=mock_instance):
            await bridge.resume_async({
                "noise_floor": 350.0,
                "user_speech_peak": 850.0,
            })

        assert bridge.conversation_state == "listening"
        assert not bridge._awaiting_calibration
        assert bridge._conversation_armed
        mock_instance.connect.assert_called_once()
        mock_instance.clear_input_buffer.assert_called_once()
        unmute_calls = [
            c for c in transport.send_message.call_args_list
            if c[0][0].type == MessageType.UNMUTE_MIC
        ]
        assert len(unmute_calls) == 1

    def test_start_resume_loopback_goes_to_listening(self) -> None:
        bridge = AudioBridge(_make_mock_transport(), loopback=True)
        bridge.start_resume()
        assert bridge.conversation_state == "listening"
        assert bridge.is_running
        assert not bridge._awaiting_calibration


class TestCalibrationWatchdog:
    async def test_repeats_prompt_while_awaiting_calibration(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("voice_assistant.audio.bridge.CALIBRATION_REPEAT_SEC", 0)
        monkeypatch.setattr("voice_assistant.audio.bridge.CALIBRATION_TIMEOUT_SEC", 999)

        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        bridge._awaiting_calibration = True
        bridge._schedule_calibration_watchdog()

        await asyncio.sleep(0.05)

        assert bridge.conversation_state == "calibrating_retry"
        bridge._cancel_calibration_watchdog()

    async def test_gives_up_after_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("voice_assistant.audio.bridge.CALIBRATION_REPEAT_SEC", 0)
        monkeypatch.setattr("voice_assistant.audio.bridge.CALIBRATION_TIMEOUT_SEC", 0)

        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        bridge._awaiting_calibration = True
        timed_out = False

        def on_timeout() -> None:
            nonlocal timed_out
            timed_out = True

        bridge.set_calibration_timeout_callback(on_timeout)
        bridge._schedule_calibration_watchdog()

        await asyncio.sleep(0.05)

        assert timed_out

    async def test_watchdog_stops_once_calibration_completes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("voice_assistant.audio.bridge.CALIBRATION_REPEAT_SEC", 0.01)
        monkeypatch.setattr("voice_assistant.audio.bridge.CALIBRATION_TIMEOUT_SEC", 999)

        bridge = AudioBridge(_make_mock_transport(), loopback=True)
        bridge._awaiting_calibration = True
        bridge._schedule_calibration_watchdog()

        bridge._awaiting_calibration = False
        await asyncio.sleep(0.03)

        assert bridge.conversation_state != "calibrating_retry"
        assert not bridge._awaiting_calibration


class TestWholeChunkPlayback:
    async def test_single_play_audio_on_response_done(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.start()
        bridge.set_device_ready(True)

        pcm1 = b"\x00\x01" * 100
        pcm2 = b"\x02\x03" * 150
        event_queue: asyncio.Queue = asyncio.Queue()
        await event_queue.put(RealtimeAudioDelta(pcm_bytes=pcm1))
        await event_queue.put(RealtimeAudioDelta(pcm_bytes=pcm2))
        await event_queue.put(RealtimeResponseDone(response_id="r1"))

        mock_client = AsyncMock()
        mock_client.is_connected = True

        async def fake_iter():
            while True:
                event = await event_queue.get()
                if event is None:
                    return
                yield event

        mock_client.iter_events = fake_iter
        bridge._realtime_client = mock_client
        bridge._event_task = asyncio.create_task(bridge._process_realtime_events())

        await asyncio.sleep(0.1)
        await event_queue.put(None)
        await bridge._event_task

        play_calls = [
            c for c in transport.send_message.call_args_list
            if c[0][0].type == MessageType.PLAY_AUDIO
        ]

        # Audio is delivered as the reply followed by a trailing silence pad,
        # split across whole 4800-byte chunks with is_final on the last only.
        delivered = b"".join(
            base64_to_pcm16(c[0][0].payload["audio"]) for c in play_calls
        )
        assert delivered == pcm1 + pcm2 + TAIL_SILENCE
        assert play_calls[-1][0][0].payload["is_final"] is True
        assert all(c[0][0].payload["is_final"] is False for c in play_calls[:-1])
        assert play_calls[0][0][0].payload["duration_ms"] > 0

        unmute_calls = [
            c for c in transport.send_message.call_args_list
            if c[0][0].type == MessageType.UNMUTE_MIC
        ]
        assert len(unmute_calls) == 0

    async def test_playback_complete_triggers_unmute(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.start()
        bridge.set_device_ready(True)
        bridge._mic_muted = True
        bridge._ai_speaking = True
        bridge._pending_playback_seq = 3

        await bridge.handle_playback_complete({
            "sequence_number": 3,
            "duration_ms": 2000,
        })

        unmute_calls = [
            c for c in transport.send_message.call_args_list
            if c[0][0].type == MessageType.UNMUTE_MIC
        ]
        assert len(unmute_calls) == 1
        assert not bridge.mic_muted
        assert bridge.conversation_state == "listening"

    async def test_stale_playback_complete_ignored(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.start()
        bridge._mic_muted = True
        bridge._pending_playback_seq = 2

        await bridge.handle_playback_complete({
            "sequence_number": 1,
            "duration_ms": 1000,
        })

        transport.send_message.assert_not_called()
        assert bridge.mic_muted


class TestPlayAudioChunking:
    async def test_large_response_is_chunked_under_one_mb(self) -> None:
        from voice_assistant.audio.utils import PLAY_AUDIO_CHUNK_BYTES
        from voice_assistant.core.message import Message

        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.start()
        bridge.set_device_ready(True)

        pcm_size = 900 * 1024
        pcm = b"\x00\x01" * (pcm_size // 2)
        event_queue: asyncio.Queue = asyncio.Queue()
        await event_queue.put(RealtimeAudioDelta(pcm_bytes=pcm))
        await event_queue.put(RealtimeResponseDone(response_id="r-large"))

        mock_client = AsyncMock()
        mock_client.is_connected = True

        async def fake_iter():
            while True:
                event = await event_queue.get()
                if event is None:
                    return
                yield event

        mock_client.iter_events = fake_iter
        bridge._realtime_client = mock_client
        bridge._event_task = asyncio.create_task(bridge._process_realtime_events())

        await asyncio.sleep(0.2)
        await event_queue.put(None)
        await bridge._event_task

        play_calls = [
            c for c in transport.send_message.call_args_list
            if c[0][0].type == MessageType.PLAY_AUDIO
        ]
        # Delivered audio includes the trailing silence pad appended at finalize.
        total = pcm_size + len(TAIL_SILENCE)
        expected_chunks = (total + PLAY_AUDIO_CHUNK_BYTES - 1) // PLAY_AUDIO_CHUNK_BYTES
        if total % PLAY_AUDIO_CHUNK_BYTES == 0:
            expected_chunks += 1  # empty is_final=True marker after exact full chunks
        assert len(play_calls) == expected_chunks

        delivered = b"".join(
            base64_to_pcm16(c[0][0].payload["audio"]) for c in play_calls
        )
        assert delivered == pcm + TAIL_SILENCE

        for call in play_calls:
            msg: Message = call[0][0]
            frame_bytes = len(msg.model_dump_json().encode("utf-8"))
            assert frame_bytes < 1_048_576, f"Frame too large: {frame_bytes} bytes"

        assert play_calls[-1][0][0].payload["is_final"] is True
        assert all(
            c[0][0].payload["is_final"] is False
            for c in play_calls[:-1]
        )

    async def test_mute_not_sent_before_device_ready(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.start()

        pcm = b"\x00\x01" * 100
        event_queue: asyncio.Queue = asyncio.Queue()
        await event_queue.put(RealtimeAudioDelta(pcm_bytes=pcm))
        await event_queue.put(RealtimeResponseDone(response_id="r1"))

        mock_client = AsyncMock()
        mock_client.is_connected = True

        async def fake_iter():
            while True:
                event = await event_queue.get()
                if event is None:
                    return
                yield event

        mock_client.iter_events = fake_iter
        bridge._realtime_client = mock_client
        bridge._event_task = asyncio.create_task(bridge._process_realtime_events())

        await asyncio.sleep(0.1)
        await event_queue.put(None)
        await bridge._event_task

        transport.send_message.assert_not_called()

    async def test_stop_async_unmutes_when_mic_muted(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.start()
        bridge.set_device_ready(True)
        bridge._mic_muted = True

        await bridge.stop_async()

        unmute_calls = [
            c for c in transport.send_message.call_args_list
            if c[0][0].type == MessageType.UNMUTE_MIC
        ]
        assert len(unmute_calls) == 1
        assert not bridge.mic_muted

    async def test_stop_async_skips_unmute_when_device_not_ready(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.start()
        bridge._mic_muted = True

        await bridge.stop_async()

        transport.send_message.assert_not_called()

    async def test_reset_on_disconnect_cancels_unmute_timeout(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.start()
        bridge.set_device_ready(True)
        bridge._mic_muted = True
        bridge._pending_playback_seq = 5
        bridge._schedule_unmute_timeout(5000)

        await bridge.reset_on_disconnect()

        assert bridge._unmute_timeout_task is None
        assert bridge._pending_playback_seq is None
        assert not bridge.device_ready
        transport.send_message.assert_not_called()


class TestAudioBridgeTranscriptOrdering:
    async def test_assistant_transcript_waits_for_user_turn(self) -> None:
        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        transcripts: list[tuple[str, str, bool]] = []
        bridge.set_transcript_callback(lambda r, t, f: transcripts.append((r, t, f)))

        event_queue: asyncio.Queue = asyncio.Queue()
        await event_queue.put(RealtimeSpeechStopped())
        await event_queue.put(
            RealtimeTranscript(role="assistant", text="Hi", final=False),
        )
        await event_queue.put(
            RealtimeTranscript(role="assistant", text="Hi there!", final=True),
        )
        await event_queue.put(
            RealtimeTranscript(role="user", text="Hello", final=True),
        )

        mock_client = AsyncMock()
        mock_client.is_connected = True

        async def fake_iter():
            while True:
                event = await event_queue.get()
                if event is None:
                    return
                yield event

        mock_client.iter_events = fake_iter
        bridge._realtime_client = mock_client
        bridge.start()
        bridge._event_task = asyncio.create_task(bridge._process_realtime_events())

        await asyncio.sleep(0.1)
        await event_queue.put(None)
        await bridge._event_task

        assert transcripts == [
            ("user", "Hello", True),
            ("assistant", "Hi", False),
            ("assistant", "Hi there!", True),
        ]

    async def test_opening_greeting_emits_assistant_without_user(self) -> None:
        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        transcripts: list[tuple[str, str, bool]] = []
        bridge.set_transcript_callback(lambda r, t, f: transcripts.append((r, t, f)))

        event_queue: asyncio.Queue = asyncio.Queue()
        await event_queue.put(
            RealtimeTranscript(role="assistant", text="Welcome!", final=True),
        )

        mock_client = AsyncMock()
        mock_client.is_connected = True

        async def fake_iter():
            while True:
                event = await event_queue.get()
                if event is None:
                    return
                yield event

        mock_client.iter_events = fake_iter
        bridge._realtime_client = mock_client
        bridge._awaiting_opening_greeting = True
        bridge.start()
        bridge._event_task = asyncio.create_task(bridge._process_realtime_events())

        await asyncio.sleep(0.1)
        await event_queue.put(None)
        await bridge._event_task

        assert transcripts == [("assistant", "Welcome!", True)]

    async def test_held_assistant_released_when_user_turn_is_echo(self) -> None:
        # If the only user transcript for the turn is filtered as echo, the
        # buffered assistant reply must still be emitted (not orphaned).
        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        transcripts: list[tuple[str, str, bool]] = []
        bridge.set_transcript_callback(lambda r, t, f: transcripts.append((r, t, f)))

        event_queue: asyncio.Queue = asyncio.Queue()
        await event_queue.put(RealtimeSpeechStopped())
        await event_queue.put(
            RealtimeTranscript(role="assistant", text="Hello there friend", final=True),
        )
        # User transcript is an echo of the assistant line → filtered out.
        await event_queue.put(
            RealtimeTranscript(role="user", text="Hello there friend", final=True),
        )

        mock_client = AsyncMock()
        mock_client.is_connected = True

        async def fake_iter():
            while True:
                event = await event_queue.get()
                if event is None:
                    return
                yield event

        mock_client.iter_events = fake_iter
        bridge._realtime_client = mock_client
        bridge.start()
        bridge._event_task = asyncio.create_task(bridge._process_realtime_events())

        await asyncio.sleep(0.1)
        await event_queue.put(None)
        await bridge._event_task

        assert ("assistant", "Hello there friend", True) in transcripts
        assert all(role != "user" for role, _, _ in transcripts)

    async def test_held_assistant_flushed_on_user_transcript_timeout(
        self, monkeypatch
    ) -> None:
        # If the user transcript never lands, the held assistant reply is still
        # released after the timeout rather than being dropped.
        import voice_assistant.audio.bridge as bridge_mod

        monkeypatch.setattr(bridge_mod, "USER_TRANSCRIPT_TIMEOUT_SEC", 0.05)

        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        transcripts: list[tuple[str, str, bool]] = []
        bridge.set_transcript_callback(lambda r, t, f: transcripts.append((r, t, f)))

        bridge._awaiting_user_transcript = True
        bridge._buffered_transcripts.append(("assistant", "Reply", True))
        bridge._schedule_user_transcript_timeout()

        await asyncio.sleep(0.15)

        assert transcripts == [("assistant", "Reply", True)]
        assert bridge._awaiting_user_transcript is False


class TestCalibrationHelloStartup:
    async def test_startup_response_not_cancelled_when_armed(self) -> None:
        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        bridge._conversation_armed = True

        mock_client = await TestOpeningListenGuard()._run_event_queue(
            bridge,
            [RealtimeResponseCreated()],
        )

        mock_client.cancel_response.assert_not_called()

    async def test_genuine_speech_arms_before_response_created(self) -> None:
        # A real user turn (speech_started not ignored) must arm the
        # conversation so the phantom-response guard doesn't cancel the reply
        # to the child's first utterance.
        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        bridge._conversation_armed = False
        bridge._explicit_greeting_pending = False

        mock_client = await TestOpeningListenGuard()._run_event_queue(
            bridge,
            [RealtimeSpeechStarted(), RealtimeResponseCreated()],
        )

        assert bridge._conversation_armed
        mock_client.cancel_response.assert_not_called()

    async def test_echo_during_ai_speech_does_not_arm(self) -> None:
        # While the AI is speaking, server-VAD speech_started is echo, not a
        # real turn: it must be ignored and must not arm the conversation.
        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        bridge._conversation_armed = False
        bridge._explicit_greeting_pending = False
        bridge._ai_speaking = True

        mock_client = await TestOpeningListenGuard()._run_event_queue(
            bridge,
            [RealtimeSpeechStarted()],
        )

        assert not bridge._conversation_armed
        _ = mock_client


class TestOpeningListenGuard:
    async def _run_event_queue(
        self,
        bridge: AudioBridge,
        events: list,
    ) -> AsyncMock:
        event_queue: asyncio.Queue = asyncio.Queue()
        for event in events:
            await event_queue.put(event)

        mock_client = AsyncMock()
        mock_client.is_connected = True

        async def fake_iter():
            while True:
                event = await event_queue.get()
                if event is None:
                    return
                yield event

        mock_client.iter_events = fake_iter
        bridge._realtime_client = mock_client
        bridge.start()
        bridge._event_task = asyncio.create_task(bridge._process_realtime_events())

        await asyncio.sleep(0.1)
        await event_queue.put(None)
        await bridge._event_task
        return mock_client

    async def test_phantom_response_cancelled_before_armed(self) -> None:
        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        bridge._conversation_armed = False
        bridge._explicit_greeting_pending = False

        mock_client = await self._run_event_queue(
            bridge,
            [RealtimeResponseCreated()],
        )

        mock_client.cancel_response.assert_called_once()
        mock_client.clear_input_buffer.assert_called_once()

    async def test_conversation_arms_on_two_char_transcript(self) -> None:
        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        bridge._opening_phase_active = True

        await self._run_event_queue(
            bridge,
            [RealtimeTranscript(role="user", text="hi", final=True)],
        )

        assert bridge._conversation_armed
        assert not bridge._opening_phase_active

    async def test_one_char_transcript_does_not_arm(self) -> None:
        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        bridge._opening_phase_active = True

        mock_client = await self._run_event_queue(
            bridge,
            [
                RealtimeTranscript(role="user", text="a", final=True),
                RealtimeResponseCreated(),
            ],
        )

        assert not bridge._conversation_armed
        mock_client.cancel_response.assert_called_once()

    async def test_opening_nudge_after_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("voice_assistant.audio.bridge.OPENING_NUDGE_WAIT_SEC", 0)

        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.start()
        bridge.set_device_ready(True)
        bridge._opening_phase_active = True

        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.clear_input_buffer = AsyncMock()
        mock_client.request_opening_greeting = AsyncMock()

        async def fake_iter():
            await asyncio.Event().wait()
            return
            yield

        mock_client.iter_events = fake_iter
        bridge._realtime_client = mock_client

        bridge._schedule_opening_nudge()
        await asyncio.sleep(0.05)

        mock_client.request_opening_greeting.assert_called_once()
        assert bridge._opening_nudge_sent

    async def test_no_nudge_after_user_arms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("voice_assistant.audio.bridge.OPENING_NUDGE_WAIT_SEC", 0)

        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        bridge.start()
        bridge._opening_phase_active = True

        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.clear_input_buffer = AsyncMock()
        mock_client.request_opening_greeting = AsyncMock()

        async def fake_iter():
            await asyncio.Event().wait()
            return
            yield

        mock_client.iter_events = fake_iter
        bridge._realtime_client = mock_client

        bridge._schedule_opening_nudge()
        bridge._arm_conversation()
        await asyncio.sleep(0.05)

        mock_client.request_opening_greeting.assert_not_called()

    async def test_no_third_greeting_on_vad_after_nudge(self) -> None:
        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        bridge._opening_phase_active = True
        bridge._opening_nudge_sent = True
        bridge._conversation_armed = False
        bridge._explicit_greeting_pending = False

        mock_client = await self._run_event_queue(
            bridge,
            [RealtimeResponseCreated()],
        )

        mock_client.cancel_response.assert_called_once()

    async def test_greeting_playback_starts_opening_wait(self) -> None:
        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        bridge.start()
        bridge.set_device_ready(True)
        bridge._opening_phase_active = True
        bridge._mic_muted = True
        bridge._pending_playback_seq = 1
        bridge._pending_greeting_playback = True

        mock_client = AsyncMock()
        mock_client.is_connected = True

        async def fake_iter():
            await asyncio.Event().wait()
            return
            yield

        mock_client.iter_events = fake_iter
        bridge._realtime_client = mock_client

        await bridge.handle_playback_complete({
            "sequence_number": 1,
            "duration_ms": 2000,
        })

        assert bridge.conversation_state == "waiting_for_kid"
        assert bridge._opening_nudge_task is not None


class TestGreetFirstStartupFlow:
    """End-to-end greet-first flow: greeting drains, then no self-conversation."""

    async def test_greeting_then_quiet_does_not_self_converse(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.start()
        bridge.set_device_ready(True)

        mock_client = AsyncMock()
        mock_client.is_connected = True
        event_queue: asyncio.Queue = asyncio.Queue()

        async def fake_iter():
            while True:
                event = await event_queue.get()
                if event is None:
                    return
                yield event

        mock_client.iter_events = fake_iter
        bridge._realtime_client = mock_client
        bridge._session_ready = True
        bridge._event_task = asyncio.create_task(bridge._process_realtime_events())

        # Assistant greets first (mic muted).
        await bridge._begin_openai_conversation()
        assert bridge.conversation_state == "greeting"

        # OpenAI streams the greeting audio, then finishes.
        await event_queue.put(RealtimeAudioDelta(pcm_bytes=b"\x01\x02" * 200))
        await event_queue.put(RealtimeResponseDone(response_id="greet"))
        await asyncio.sleep(0.05)

        # The greeting arms the waiting_for_kid transition on playback complete.
        assert bridge._pending_greeting_playback is True
        assert not bridge._conversation_armed

        seq = bridge._pending_playback_seq
        await bridge.handle_playback_complete({"sequence_number": seq, "duration_ms": 500})
        assert bridge.conversation_state == "waiting_for_kid"

        # Child stays quiet; echo/noise makes OpenAI auto-create a response.
        # Because nothing is armed, it must be cancelled -- no self-conversation.
        await event_queue.put(RealtimeResponseCreated())
        await asyncio.sleep(0.05)
        mock_client.cancel_response.assert_called_once()

        await event_queue.put(None)
        await bridge._event_task

    async def test_child_first_turn_after_greeting_is_answered(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.start()
        bridge.set_device_ready(True)
        bridge._opening_phase_active = True

        mock_client = AsyncMock()
        mock_client.is_connected = True
        event_queue: asyncio.Queue = asyncio.Queue()

        async def fake_iter():
            while True:
                event = await event_queue.get()
                if event is None:
                    return
                yield event

        mock_client.iter_events = fake_iter
        bridge._realtime_client = mock_client
        bridge._event_task = asyncio.create_task(bridge._process_realtime_events())

        # Child genuinely speaks: speech_started arms before response.created,
        # so the phantom guard does NOT cancel the answer.
        await event_queue.put(RealtimeSpeechStarted())
        await event_queue.put(RealtimeSpeechStopped())
        await event_queue.put(RealtimeResponseCreated())
        await asyncio.sleep(0.05)

        assert bridge._conversation_armed
        mock_client.cancel_response.assert_not_called()

        await event_queue.put(None)
        await bridge._event_task


class TestSessionManagerBridgeIntegration:
    async def test_bridge_created_on_start_conversation(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        await sm.wait_for_device()
        await sm.start_conversation()

        assert sm._audio_bridge is not None
        assert sm._audio_bridge.is_running

    async def test_bridge_stopped_on_stop_conversation(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        await sm.wait_for_device()
        await sm.start_conversation()
        await sm.stop_conversation()

        assert sm._audio_bridge is None

    async def test_bridge_loopback_flag_passed(self) -> None:
        from voice_assistant.config import Config

        t = MockTransport()
        config = Config(openai_api_key="")
        sm = SessionManager(t, loopback=False, config=config)
        await sm.wait_for_device()
        await sm.start_conversation()

        assert sm._audio_bridge is not None
        assert sm._audio_bridge.loopback is True

    async def test_session_loop_with_bridge(self) -> None:
        t = MockTransport()
        sm = SessionManager(t, max_iterations=5, loopback=True)
        await sm.run_session_loop()
        assert sm.state == SessionState.SHUTDOWN
