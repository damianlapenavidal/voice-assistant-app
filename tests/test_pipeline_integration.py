"""Integration tests: mock transport + mock RealtimeClient pipeline.

Verifies that AUDIO_FRAME flows through AudioBridge to RealtimeClient.send_audio,
and that RealtimeAudioDelta events flow back as PLAY_AUDIO messages.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voice_assistant.audio.bridge import AudioBridge
from voice_assistant.audio.utils import generate_test_tone, pcm16_to_base64
from voice_assistant.config import Config
from voice_assistant.core.message import MessageType
from voice_assistant.core.session import SessionManager, SessionState
from voice_assistant.openai_client.realtime import (
    RealtimeAudioDelta,
    RealtimeClient,
    RealtimeResponseDone,
    RealtimeTranscript,
)
from voice_assistant.transport.base import Transport
from voice_assistant.transport.mock_transport import MockTransport


def _make_mock_transport() -> Transport:
    t = AsyncMock(spec=Transport)
    t.is_connected = True
    return t


def _frame_payload(seq: int = 1, audio_b64: str | None = None) -> dict:
    if audio_b64 is None:
        audio_b64 = pcm16_to_base64(generate_test_tone(50))
    return {"audio": audio_b64, "sequence_number": seq, "timestamp": "2025-01-01T00:00:00Z"}


class TestAudioBridgeOpenAIMode:
    """AudioBridge with loopback=False forwards audio to RealtimeClient."""

    async def test_openai_mode_sends_to_realtime_client(self) -> None:
        transport = _make_mock_transport()
        config = Config(openai_api_key="test-key")
        bridge = AudioBridge(transport, loopback=False, config=config)

        mock_client = AsyncMock(spec=RealtimeClient)
        mock_client.is_connected = True

        async def fake_iter() -> AsyncIterator:
            await asyncio.Event().wait()
            return
            yield

        mock_client.iter_events = fake_iter

        bridge._realtime_client = mock_client
        bridge.start()
        bridge._event_task = asyncio.create_task(bridge._process_realtime_events())

        pcm = generate_test_tone(50)
        audio_b64 = pcm16_to_base64(pcm)
        await bridge.handle_audio_frame(_frame_payload(seq=1, audio_b64=audio_b64))

        mock_client.send_audio.assert_called_once()
        sent_pcm = mock_client.send_audio.call_args[0][0]
        assert sent_pcm == pcm

        transport.send_message.assert_not_called()
        await bridge.stop_async()

    async def test_openai_mode_relays_audio_delta_as_play_audio(self) -> None:
        transport = _make_mock_transport()
        config = Config(openai_api_key="test-key")
        bridge = AudioBridge(transport, loopback=False, config=config)

        pcm_response = generate_test_tone(30)
        event_queue: asyncio.Queue = asyncio.Queue()
        await event_queue.put(RealtimeAudioDelta(pcm_bytes=pcm_response))
        await event_queue.put(RealtimeResponseDone(response_id="resp-1"))

        mock_client = AsyncMock(spec=RealtimeClient)
        mock_client.is_connected = True

        async def fake_iter() -> AsyncIterator:
            while True:
                event = await event_queue.get()
                if event is None:
                    return
                yield event

        mock_client.iter_events = fake_iter

        bridge._realtime_client = mock_client
        bridge.start()
        bridge.set_device_ready(True)
        bridge._event_task = asyncio.create_task(bridge._process_realtime_events())

        await asyncio.sleep(0.15)
        await event_queue.put(None)
        await asyncio.sleep(0.15)

        calls = transport.send_message.call_args_list
        mute_calls = [c for c in calls if c[0][0].type == MessageType.MUTE_MIC]
        play_calls = [c for c in calls if c[0][0].type == MessageType.PLAY_AUDIO]
        assert len(mute_calls) == 1, f"Expected 1 MUTE_MIC, got {len(mute_calls)}"
        assert len(play_calls) == 1, f"Expected 1 PLAY_AUDIO, got {len(play_calls)}"
        sent_msg = play_calls[0][0][0]
        assert sent_msg.payload["audio"] == pcm16_to_base64(pcm_response)
        assert sent_msg.payload["is_final"] is True

        unmute_calls = [c for c in calls if c[0][0].type == MessageType.UNMUTE_MIC]
        assert len(unmute_calls) == 0

        await bridge.stop_async()

    async def test_playback_complete_unmutes_via_session(self) -> None:
        from voice_assistant.core.message import create_message

        t = MockTransport()
        config = Config(openai_api_key="test-key")
        sm = SessionManager(t, loopback=False, config=config)
        await sm.wait_for_device()

        mock_instance = AsyncMock()
        mock_instance.is_connected = True

        async def fake_iter() -> AsyncIterator:
            await asyncio.Event().wait()
            return
            yield

        mock_instance.iter_events = fake_iter

        import voice_assistant.openai_client.realtime as rt_mod

        with patch.object(rt_mod, "RealtimeClient", return_value=mock_instance):
            await sm.start_conversation()

        assert sm._audio_bridge is not None
        sm._audio_bridge._mic_muted = True
        sm._audio_bridge._pending_playback_seq = 7

        msg = create_message(
            MessageType.PLAYBACK_COMPLETE,
            {"sequence_number": 7, "duration_ms": 1500},
        )
        await sm._process_message(msg, 1)

        assert not sm._audio_bridge.mic_muted
        await sm.stop_conversation()

    async def test_openai_mode_emits_transcript_callback(self) -> None:
        transport = _make_mock_transport()
        config = Config(openai_api_key="test-key")
        bridge = AudioBridge(transport, loopback=False, config=config)

        transcripts: list[tuple[str, str, bool]] = []
        bridge.set_transcript_callback(lambda r, t, f: transcripts.append((r, t, f)))

        event_queue: asyncio.Queue = asyncio.Queue()
        await event_queue.put(
            RealtimeTranscript(role="user", text="Hello", final=True),
        )
        await event_queue.put(
            RealtimeTranscript(role="assistant", text="Hi there!", final=True),
        )

        mock_client = AsyncMock(spec=RealtimeClient)
        mock_client.is_connected = True

        async def fake_iter() -> AsyncIterator:
            while True:
                event = await event_queue.get()
                if event is None:
                    return
                yield event

        mock_client.iter_events = fake_iter

        bridge._realtime_client = mock_client
        bridge.start()
        bridge._event_task = asyncio.create_task(bridge._process_realtime_events())

        await asyncio.sleep(0.05)
        await event_queue.put(None)
        await asyncio.sleep(0.05)

        assert len(transcripts) == 2
        assert transcripts[0] == ("user", "Hello", True)
        assert transcripts[1] == ("assistant", "Hi there!", True)

        await bridge.stop_async()

    async def test_loopback_mode_does_not_touch_realtime(self) -> None:
        transport = _make_mock_transport()
        bridge = AudioBridge(transport, loopback=True)
        bridge.start()

        assert bridge._realtime_client is None
        await bridge.handle_audio_frame(_frame_payload(seq=1))

        transport.send_message.assert_called_once()
        sent_msg = transport.send_message.call_args[0][0]
        assert sent_msg.type == MessageType.PLAY_AUDIO

        bridge.stop()


class TestSessionManagerOpenAIIntegration:
    """SessionManager wires config and loopback decision correctly."""

    async def test_session_defaults_to_loopback_without_api_key(self) -> None:
        t = MockTransport()
        config = Config(openai_api_key="")
        sm = SessionManager(t, loopback=False, config=config)
        await sm.wait_for_device()
        await sm.start_conversation()

        assert sm._audio_bridge is not None
        assert sm._audio_bridge.loopback is True
        assert sm.active_mode == "loopback"

        await sm.stop_conversation()

    async def test_session_uses_openai_mode_with_api_key(self) -> None:
        import voice_assistant.openai_client.realtime as rt_mod

        t = MockTransport()
        config = Config(openai_api_key="test-key")
        sm = SessionManager(t, loopback=False, config=config)
        await sm.wait_for_device()

        mock_instance = AsyncMock()
        mock_instance.is_connected = True

        async def fake_iter() -> AsyncIterator:
            await asyncio.Event().wait()
            return
            yield

        mock_instance.iter_events = fake_iter
        mock_instance.connect = AsyncMock()
        mock_instance.update_vad_settings = AsyncMock()

        with patch.object(rt_mod, "RealtimeClient", return_value=mock_instance):
            await sm.start_conversation()

            assert sm._audio_bridge is not None
            assert sm._audio_bridge.loopback is False
            assert sm.active_mode == "openai"
            await asyncio.sleep(0)
            mock_instance.connect.assert_called_once_with(send_session_update=False)

            await sm._audio_bridge.handle_calibration_complete({
                "noise_floor": 400.0,
                "user_speech_peak": 800.0,
            })
            mock_instance.update_vad_settings.assert_called_once()

            # Greet-first: the assistant opens with a greeting once calibrated.
            mock_instance.clear_input_buffer.assert_called_once()
            mock_instance.request_opening_greeting.assert_called_once()

            await sm.stop_conversation()
            mock_instance.disconnect.assert_called_once()

    async def test_forced_loopback_overrides_api_key(self) -> None:
        t = MockTransport()
        config = Config(openai_api_key="test-key")
        sm = SessionManager(t, loopback=True, config=config)
        await sm.wait_for_device()
        await sm.start_conversation()

        assert sm._audio_bridge is not None
        assert sm._audio_bridge.loopback is True

        await sm.stop_conversation()

    async def test_transcript_events_emitted_through_session(self) -> None:
        t = MockTransport()
        config = Config(openai_api_key="")
        sm = SessionManager(t, config=config)
        events: list[tuple[str, dict]] = []
        sm.add_event_listener(lambda e, d: events.append((e, d)))

        await sm.wait_for_device()
        await sm.start_conversation()

        sm._on_transcript("user", "Hello", True)
        sm._on_transcript("assistant", "Hi!", True)

        transcript_events = [(e, d) for e, d in events if e == "transcript"]
        assert len(transcript_events) == 2
        assert transcript_events[0][1] == {"role": "user", "text": "Hello", "final": True}
        assert transcript_events[1][1] == {"role": "assistant", "text": "Hi!", "final": True}

        await sm.stop_conversation()

    async def test_conversation_state_property(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)

        assert sm.conversation_state == "idle"

        await sm.wait_for_device()
        await sm.start_conversation()
        assert sm.conversation_state == "calibrating"

        assert sm._audio_bridge is not None
        await sm._audio_bridge.handle_calibration_complete({
            "noise_floor": 300.0,
            "user_speech_peak": 900.0,
        })
        assert sm.conversation_state == "listening"

        await sm.stop_conversation()
        assert sm.conversation_state == "idle"
