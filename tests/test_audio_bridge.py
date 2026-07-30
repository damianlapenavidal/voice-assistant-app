"""Tests for AudioBridge loopback relay and OpenAI whole-chunk playback."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from voice_assistant.audio.bridge import AudioBridge, TAIL_SILENCE
from voice_assistant.audio.utils import as_pcm_bytes, generate_silence, pcm16_to_base64
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
            as_pcm_bytes(c[0][0].payload["audio"]) for c in play_calls
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
            as_pcm_bytes(c[0][0].payload["audio"]) for c in play_calls
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


class TestBinaryAndJsonAudioPayloads:
    """handle_audio_frame accepts either payload representation.

    Base64 str is what a JSON AUDIO_FRAME carries; raw bytes is what the
    binary path hands over. Both must reach OpenAI as identical PCM.
    """

    @pytest.mark.parametrize("as_binary", [False, True], ids=["base64_str", "raw_bytes"])
    async def test_audio_forwarded_identically_either_way(self, as_binary: bool) -> None:
        pcm = b"\x01\x02\x03\x04\x05\x06"
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)

        realtime = AsyncMock()
        realtime.is_connected = True
        bridge._realtime_client = realtime
        bridge.start()

        audio = pcm if as_binary else pcm16_to_base64(pcm)
        await bridge.handle_audio_frame(_frame_payload(audio=audio))

        realtime.send_audio.assert_awaited_once_with(pcm)

    async def test_play_audio_chunk_carries_raw_pcm(self) -> None:
        """The bridge hands raw PCM to the transport, which owns the encode --
        so a binary-framed device never pays for a base64 round trip."""
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.set_device_ready(True)

        pcm = b"\xAA\xBB" * 8
        await bridge._send_play_audio_chunk(pcm, is_final=True)

        sent = transport.send_message.call_args[0][0]
        assert sent.type == MessageType.PLAY_AUDIO
        assert sent.payload["audio"] == pcm
        assert sent.payload["is_final"] is True


class TestHandleAudioGap:
    """AUDIO_GAP synthesizes the silence a device elided (Phase 5b)."""

    async def test_synthesizes_and_forwards_exact_silence(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.start()

        realtime = AsyncMock()
        realtime.is_connected = True
        bridge._realtime_client = realtime

        await bridge.handle_audio_gap({"duration_ms": 500, "sequence_number": 10})

        realtime.send_audio.assert_awaited_once_with(generate_silence(500))

    async def test_noop_when_not_running(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        realtime = AsyncMock()
        realtime.is_connected = True
        bridge._realtime_client = realtime

        await bridge.handle_audio_gap({"duration_ms": 500, "sequence_number": 1})

        realtime.send_audio.assert_not_awaited()

    async def test_noop_during_calibration(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.start()
        bridge._awaiting_calibration = True
        realtime = AsyncMock()
        realtime.is_connected = True
        bridge._realtime_client = realtime

        await bridge.handle_audio_gap({"duration_ms": 500, "sequence_number": 1})

        realtime.send_audio.assert_not_awaited()

    async def test_noop_in_loopback(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=True)
        bridge.start()

        await bridge.handle_audio_gap({"duration_ms": 500, "sequence_number": 1})

        transport.send_message.assert_not_called()

    async def test_noop_when_duration_not_positive(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False)
        bridge.start()
        realtime = AsyncMock()
        realtime.is_connected = True
        bridge._realtime_client = realtime

        await bridge.handle_audio_gap({"duration_ms": 0, "sequence_number": 1})

        realtime.send_audio.assert_not_awaited()


def _barge_in_config(enabled: bool = True):
    from voice_assistant.config import Config

    return Config(openai_api_key="", barge_in=enabled)


def _speech(ms: int) -> bytes:
    """PCM loud enough to clear BARGE_IN_SILENCE_RMS."""
    import struct

    from voice_assistant.audio.bridge import BYTE_RATE

    samples = int(BYTE_RATE * ms / 1000) // 2
    return struct.pack("<h", 8000) * samples


def _quiet(ms: int) -> bytes:
    from voice_assistant.audio.bridge import BYTE_RATE

    return b"\x00" * (int(BYTE_RATE * ms / 1000) & ~1)


class TestBargeInGapDetection:
    """Phase 6a: segment the reply at a pause, listen while the device drains."""

    def test_disabled_by_default(self) -> None:
        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        assert bridge._barge_in_enabled is False

    def test_disabled_in_loopback_even_when_flag_is_on(self) -> None:
        """Loopback has no assistant reply to interrupt, so the flag can't apply."""
        bridge = AudioBridge(
            _make_mock_transport(), loopback=True, config=_barge_in_config(True),
        )
        assert bridge._barge_in_enabled is False

    def test_boundary_needs_a_long_enough_run_of_quiet(self) -> None:
        from voice_assistant.audio.bridge import (
            BARGE_IN_MIN_GAP_CHUNKS,
            PLAY_AUDIO_CHUNK_BYTES,
        )

        bridge = AudioBridge(
            _make_mock_transport(), loopback=False, config=_barge_in_config(),
        )
        quiet_chunk = b"\x00" * PLAY_AUDIO_CHUNK_BYTES
        bridge._segment_ms_sent = 5000  # well past the min-segment floor

        for _ in range(BARGE_IN_MIN_GAP_CHUNKS - 1):
            assert bridge._is_barge_in_boundary(quiet_chunk) is False
        assert bridge._is_barge_in_boundary(quiet_chunk) is True

    def test_speech_resets_the_quiet_run(self) -> None:
        from voice_assistant.audio.bridge import (
            BARGE_IN_MIN_GAP_CHUNKS,
            PLAY_AUDIO_CHUNK_BYTES,
        )
        import struct

        bridge = AudioBridge(
            _make_mock_transport(), loopback=False, config=_barge_in_config(),
        )
        quiet_chunk = b"\x00" * PLAY_AUDIO_CHUNK_BYTES
        loud_chunk = struct.pack("<h", 8000) * (PLAY_AUDIO_CHUNK_BYTES // 2)
        bridge._segment_ms_sent = 5000

        for _ in range(BARGE_IN_MIN_GAP_CHUNKS - 1):
            bridge._is_barge_in_boundary(quiet_chunk)
        assert bridge._is_barge_in_boundary(loud_chunk) is False
        # Run restarted: one more quiet chunk must not be enough on its own.
        assert bridge._is_barge_in_boundary(quiet_chunk) is False

    def test_no_boundary_when_flag_is_off(self) -> None:
        from voice_assistant.audio.bridge import (
            BARGE_IN_MIN_GAP_CHUNKS,
            PLAY_AUDIO_CHUNK_BYTES,
        )

        bridge = AudioBridge(_make_mock_transport(), loopback=False)
        quiet_chunk = b"\x00" * PLAY_AUDIO_CHUNK_BYTES

        for _ in range(BARGE_IN_MIN_GAP_CHUNKS + 4):
            assert bridge._is_barge_in_boundary(quiet_chunk) is False

    def test_min_segment_floor_counts_delivered_audio_not_wall_clock(self) -> None:
        """The floor has to be measured in audio actually delivered. Responses
        stream out of OpenAI far faster than real time, so a wall-clock floor
        would read ~0 ms for an entire reply and never suppress anything."""
        from voice_assistant.audio.bridge import (
            BARGE_IN_MIN_GAP_CHUNKS,
            BARGE_IN_MIN_SEGMENT_MS,
            PLAY_AUDIO_CHUNK_BYTES,
        )

        bridge = AudioBridge(
            _make_mock_transport(), loopback=False, config=_barge_in_config(),
        )
        quiet_chunk = b"\x00" * PLAY_AUDIO_CHUNK_BYTES

        # Barely any audio delivered in this segment yet: no boundary, however
        # long the pause runs.
        bridge._segment_ms_sent = BARGE_IN_MIN_SEGMENT_MS - 100
        for _ in range(BARGE_IN_MIN_GAP_CHUNKS + 4):
            assert bridge._is_barge_in_boundary(quiet_chunk) is False

        # Same pause, once enough of the reply has actually been delivered.
        bridge._segment_ms_sent = BARGE_IN_MIN_SEGMENT_MS
        bridge._quiet_chunk_run = 0
        for _ in range(BARGE_IN_MIN_GAP_CHUNKS - 1):
            assert bridge._is_barge_in_boundary(quiet_chunk) is False
        assert bridge._is_barge_in_boundary(quiet_chunk) is True


class TestBargeInWindow:
    """The listening window itself: opened by the drained segment's reply."""

    async def _bridge_mid_response(self):
        transport = _make_mock_transport()
        bridge = AudioBridge(
            transport, loopback=False, config=_barge_in_config(),
        )
        bridge.start()
        bridge.set_device_ready(True)
        bridge._ai_speaking = True
        bridge._mic_muted = True
        return transport, bridge

    async def test_playback_complete_at_a_segment_opens_the_window(self) -> None:
        transport, bridge = await self._bridge_mid_response()
        bridge._awaiting_segment_playback = True
        bridge._pending_playback_seq = 7

        await bridge.handle_playback_complete({"sequence_number": 7, "duration_ms": 500})
        await asyncio.sleep(0)

        assert bridge._listening_for_barge_in is True
        # Mic released so the device streams during the pause.
        unmutes = [
            c for c in transport.send_message.call_args_list
            if c[0][0].type == MessageType.UNMUTE_MIC
        ]
        assert len(unmutes) == 1
        # Still the same assistant turn -- not the end-of-reply path.
        assert bridge._ai_speaking is True
        bridge._cancel_listen_window()

    async def test_window_closes_and_resumes_the_reply(self) -> None:
        from voice_assistant.audio.bridge import PLAY_AUDIO_CHUNK_BYTES

        transport, bridge = await self._bridge_mid_response()
        bridge._awaiting_segment_playback = True
        bridge._pending_playback_seq = 7
        bridge._audio_seq = 7
        # The rest of the reply, held back while the window is open.
        held = _speech(200)
        async with bridge._buffer_lock:
            bridge._audio_buffer.extend(held)

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "voice_assistant.audio.bridge.BARGE_IN_WINDOW_MS", 20,
        ):
            await bridge.handle_playback_complete(
                {"sequence_number": 7, "duration_ms": 500},
            )
            # Nothing may go out while the window is open.
            await asyncio.sleep(0.005)
            assert not [
                c for c in transport.send_message.call_args_list
                if c[0][0].type == MessageType.PLAY_AUDIO
            ]
            await asyncio.sleep(0.08)

        assert bridge._listening_for_barge_in is False
        assert bridge._audio_seq == 8  # next segment gets its own sequence
        mutes = [
            c for c in transport.send_message.call_args_list
            if c[0][0].type == MessageType.MUTE_MIC
        ]
        assert len(mutes) == 1  # re-muted once the window closed
        played = [
            c for c in transport.send_message.call_args_list
            if c[0][0].type == MessageType.PLAY_AUDIO
        ]
        assert played, "held audio was never resumed after the window"
        delivered = b"".join(as_pcm_bytes(c[0][0].payload["audio"]) for c in played)
        assert delivered == held[: len(delivered)]
        assert len(delivered) % PLAY_AUDIO_CHUNK_BYTES == 0

    async def test_vad_is_trusted_only_inside_the_window(self) -> None:
        _transport, bridge = await self._bridge_mid_response()

        # Mid-speech, no window: VAD is the device's own echo, ignore it.
        assert bridge._should_ignore_live_speech_vad() is True

        bridge._listening_for_barge_in = True
        # Window open: the segment drained, so the speaker is quiet and VAD
        # firing here is the user.
        assert bridge._should_ignore_live_speech_vad() is False

    async def test_barge_in_drops_the_rest_of_the_reply(self) -> None:
        _transport, bridge = await self._bridge_mid_response()
        bridge._listening_for_barge_in = True
        async with bridge._buffer_lock:
            bridge._audio_buffer.extend(_speech(500))

        await bridge._handle_barge_in()

        assert bridge._barge_in_detected is True
        assert bridge._listening_for_barge_in is False
        assert len(bridge._audio_buffer) == 0
        assert bridge._ai_speaking is False
        # The user is talking -- the mic must stay open.
        assert bridge._mic_muted is False

    async def test_deltas_after_a_barge_in_are_discarded(self) -> None:
        """OpenAI keeps streaming briefly before its cancel lands; that audio
        must not re-mute the mic and talk over the user."""
        transport, bridge = await self._bridge_mid_response()
        bridge._barge_in_detected = True

        event_queue: asyncio.Queue = asyncio.Queue()
        await event_queue.put(RealtimeAudioDelta(pcm_bytes=_speech(300)))
        await event_queue.put(RealtimeResponseDone(response_id="r-interrupted"))

        realtime = AsyncMock()
        realtime.is_connected = True

        async def fake_iter():
            while True:
                event = await event_queue.get()
                if event is None:
                    return
                yield event

        realtime.iter_events = fake_iter
        bridge._realtime_client = realtime
        task = asyncio.create_task(bridge._process_realtime_events())
        await asyncio.sleep(0.05)
        await event_queue.put(None)
        await task

        assert not [
            c for c in transport.send_message.call_args_list
            if c[0][0].type == MessageType.PLAY_AUDIO
        ]
        # Flag cleared by response.done, so the *next* reply is handled normally.
        assert bridge._barge_in_detected is False

    async def test_finalize_during_a_window_is_deferred_not_dropped(self) -> None:
        """A reply that finishes generating mid-window still gets delivered --
        after the window, never into a speaker it needs quiet."""
        transport, bridge = await self._bridge_mid_response()
        bridge._listening_for_barge_in = True
        async with bridge._buffer_lock:
            bridge._audio_buffer.extend(_speech(100))

        await bridge._finalize_response_audio()

        assert bridge._finalize_deferred is True
        assert not [
            c for c in transport.send_message.call_args_list
            if c[0][0].type == MessageType.PLAY_AUDIO
        ]
        assert len(bridge._audio_buffer) > 0  # held, not discarded

    async def test_reset_on_disconnect_drops_an_open_window(self) -> None:
        _transport, bridge = await self._bridge_mid_response()
        bridge._awaiting_segment_playback = True
        bridge._pending_playback_seq = 3
        await bridge.handle_playback_complete({"sequence_number": 3, "duration_ms": 100})
        await asyncio.sleep(0.01)  # let the window task actually start
        assert bridge._listening_for_barge_in is True

        await bridge.reset_on_disconnect()

        assert bridge._listening_for_barge_in is False
        assert bridge._listen_window_task is None


class TestChunkRms:
    def test_matches_the_device_definition(self) -> None:
        """Same function shape as the devices' audio_gating.chunk_rms, so a
        threshold means the same thing on both sides of the wire."""
        import struct

        from voice_assistant.audio.utils import chunk_rms

        assert chunk_rms(b"") == 0.0
        assert chunk_rms(b"\x00\x00" * 100) == 0.0
        loud = struct.pack("<h", 20000) * 100
        assert chunk_rms(loud) == 20000.0
        assert chunk_rms(loud, stride=4) == 20000.0


class TestBargeInEndToEnd:
    """The whole Phase 6a loop: a reply with a pause in it, segmented, listened
    in, and then either resumed or interrupted."""

    async def _run(self, *, interrupt: bool):
        from unittest.mock import patch

        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=False, config=_barge_in_config())
        bridge.start()
        bridge.set_device_ready(True)

        # A reply shaped like real TTS: a sentence, a 500 ms sentence pause, a
        # second sentence. The pause is what Phase 6a listens in. The first
        # sentence has to clear BARGE_IN_MIN_SEGMENT_MS of *delivered audio*
        # before a boundary is allowed at all.
        reply = _speech(1600) + _quiet(500) + _speech(600)
        event_queue: asyncio.Queue = asyncio.Queue()
        await event_queue.put(RealtimeAudioDelta(pcm_bytes=reply))

        realtime = AsyncMock()
        realtime.is_connected = True

        async def fake_iter():
            while True:
                event = await event_queue.get()
                if event is None:
                    return
                yield event

        realtime.iter_events = fake_iter
        bridge._realtime_client = realtime

        def play_calls():
            return [
                c for c in transport.send_message.call_args_list
                if c[0][0].type == MessageType.PLAY_AUDIO
            ]

        with patch("voice_assistant.audio.bridge.BARGE_IN_WINDOW_MS", 30):
            bridge._event_task = asyncio.create_task(bridge._process_realtime_events())
            await asyncio.sleep(0.05)

            # The pause should have ended a segment: a final chunk, then silence
            # on the wire while the app waits for the device to drain.
            finals = [c for c in play_calls() if c[0][0].payload["is_final"]]
            assert len(finals) == 1, "the pause did not end a segment"
            assert bridge._awaiting_segment_playback is True
            segment_seq = finals[0][0][0].payload["sequence_number"]
            sent_before_window = len(play_calls())

            # The device drains and replies -- this is what opens the window.
            await bridge.handle_playback_complete(
                {"sequence_number": segment_seq, "duration_ms": 600},
            )
            await asyncio.sleep(0.01)
            assert bridge._listening_for_barge_in is True
            assert len(play_calls()) == sent_before_window, "sent into an open window"

            if interrupt:
                await bridge._handle_barge_in()

            await event_queue.put(RealtimeResponseDone(response_id="r1"))
            await asyncio.sleep(0.15)
            await event_queue.put(None)
            await bridge._event_task

        bridge._cancel_unmute_timeout()
        return transport, bridge, play_calls(), reply

    async def test_reply_resumes_when_nobody_interrupts(self) -> None:
        transport, bridge, calls, reply = await self._run(interrupt=False)

        assert bridge._listening_for_barge_in is False
        delivered = b"".join(as_pcm_bytes(c[0][0].payload["audio"]) for c in calls)
        # Everything OpenAI produced still reaches the speaker, in order, with
        # the usual tail pad -- segmenting must not lose or reorder audio.
        assert delivered == reply + TAIL_SILENCE
        assert calls[-1][0][0].payload["is_final"] is True
        # Two segments were cut, so two sequence numbers were used.
        seqs = {c[0][0].payload["sequence_number"] for c in calls}
        assert len(seqs) == 2
        unmutes = [
            c for c in transport.send_message.call_args_list
            if c[0][0].type == MessageType.UNMUTE_MIC
        ]
        assert len(unmutes) == 1  # exactly one listening window

    async def test_interrupted_reply_is_abandoned_after_the_pause(self) -> None:
        _transport, bridge, calls, reply = await self._run(interrupt=True)

        delivered = b"".join(as_pcm_bytes(c[0][0].payload["audio"]) for c in calls)
        # The user cut in during the pause, so the second phrase never plays.
        assert len(delivered) < len(reply)
        assert delivered == reply[: len(delivered)]
        assert bridge._ai_speaking is False
        assert bridge._mic_muted is False  # the user has the floor
        assert bridge._barge_in_detected is False  # cleared by response.done
        assert len(bridge._audio_buffer) == 0
