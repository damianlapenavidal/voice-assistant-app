"""Tests for session manager lifecycle."""

import asyncio

import pytest

from voice_assistant.core.session import SessionManager, SessionState
from voice_assistant.transport.base import TransportError
from voice_assistant.transport.mock_transport import MockTransport


class TestSessionManagerCreation:
    """SessionManager creation with a MockTransport."""

    def test_initial_state_is_idle(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        assert sm.state == SessionState.IDLE

    def test_session_id_initially_none(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        assert sm.session_id is None


class TestDeviceConnection:
    """wait_for_device() connects and performs the HELLO handshake."""

    async def test_wait_for_device_completes_handshake(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        await sm.wait_for_device()

        assert sm.state == SessionState.ACTIVE
        assert sm.session_id is not None
        assert t.is_connected

    async def test_wait_for_device_assigns_uuid_session_id(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        await sm.wait_for_device()
        assert len(sm.session_id) == 36  # UUID format


class TestConversation:
    """start_conversation() / stop_conversation() transition states."""

    async def test_start_conversation(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        await sm.wait_for_device()
        await sm.start_conversation()
        assert sm.state == SessionState.STREAMING

    async def test_stop_conversation_returns_to_active(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        await sm.wait_for_device()
        await sm.start_conversation()
        await sm.stop_conversation()
        assert sm.state == SessionState.ACTIVE

    async def test_stop_conversation_unmutes_device_when_mic_muted(self) -> None:
        from unittest.mock import AsyncMock, patch

        import voice_assistant.openai_client.realtime as rt_mod
        from voice_assistant.config import Config
        from voice_assistant.core.message import MessageType

        t = MockTransport()
        sm = SessionManager(t, loopback=False, config=Config(openai_api_key="test-key"))
        await sm.wait_for_device()

        mock_instance = AsyncMock()
        mock_instance.is_connected = True

        async def fake_iter():
            await asyncio.Event().wait()
            return
            yield

        mock_instance.iter_events = fake_iter
        mock_instance.connect = AsyncMock()
        mock_instance.update_vad_settings = AsyncMock()

        with patch.object(rt_mod, "RealtimeClient", return_value=mock_instance):
            await sm.start_conversation()
            assert sm._audio_bridge is not None
            sm._audio_bridge._mic_muted = True

            await sm.stop_conversation()

        sent_types = [msg.type for msg in t.sent_messages]
        assert MessageType.UNMUTE_MIC in sent_types
        assert MessageType.STOP_AUDIO_STREAM in sent_types
        assert sent_types.index(MessageType.UNMUTE_MIC) < sent_types.index(
            MessageType.STOP_AUDIO_STREAM,
        )

    async def test_start_stop_start_cycle(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        await sm.wait_for_device()
        await sm.start_conversation()
        await sm.stop_conversation()
        await sm.start_conversation()
        assert sm.state == SessionState.STREAMING

    async def test_start_stop_start_skips_recalibration(self) -> None:
        from unittest.mock import AsyncMock, patch

        import voice_assistant.openai_client.realtime as rt_mod
        from voice_assistant.audio.utils import pcm16_to_base64
        from voice_assistant.config import Config
        from voice_assistant.core.message import MessageType, create_message

        t = MockTransport()
        sm = SessionManager(t, loopback=False, config=Config(openai_api_key="test-key"))
        await sm.wait_for_device()

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
        mock_instance.commit_input_buffer = AsyncMock()

        hello_pcm = b"\x00\x01" * 2400
        with patch.object(rt_mod, "RealtimeClient", return_value=mock_instance):
            await sm.start_conversation()
            cal_msg = create_message(
                MessageType.CALIBRATION_COMPLETE,
                {
                    "noise_floor": 350.0,
                    "user_speech_peak": 850.0,
                    "hello_audio": pcm16_to_base64(hello_pcm),
                },
            )
            await sm._process_message(cal_msg, 1)
            assert sm._calibration_metrics is not None

            await sm.stop_conversation()
            mock_instance.disconnect.assert_called_once()

            await sm.start_conversation()
            assert sm.state == SessionState.STREAMING
            assert sm._audio_bridge is not None
            assert sm._audio_bridge.conversation_state == "listening"
            assert not sm._audio_bridge._awaiting_calibration

            start_msgs = [
                m for m in t.sent_messages
                if m.type == MessageType.START_AUDIO_STREAM
            ]
            assert len(start_msgs) == 2
            assert start_msgs[0].payload is None
            assert start_msgs[1].payload == {"skip_calibration": True}

            status_msg = create_message(
                MessageType.CALIBRATION_STATUS,
                {"phase": "quiet"},
            )
            await sm._process_message(status_msg, 2)
            assert sm._audio_bridge.conversation_state == "listening"

    async def test_calibration_cache_cleared_on_disconnect(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        await sm.wait_for_device()
        sm._calibration_metrics = {"noise_floor": 300.0, "user_speech_peak": 900.0}

        await sm._handle_device_disconnect("test")

        assert sm._calibration_metrics is None

    async def test_start_conversation_requires_active(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        with pytest.raises(TransportError):
            await sm.start_conversation()

    async def test_stop_conversation_requires_streaming(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        await sm.wait_for_device()
        with pytest.raises(TransportError):
            await sm.stop_conversation()


class TestSessionLoop:
    """run_session_loop() runs through a complete mock lifecycle."""

    async def test_loop_completes_with_small_iterations(self) -> None:
        t = MockTransport()
        sm = SessionManager(t, max_iterations=5)
        await sm.run_session_loop()

        assert sm.state == SessionState.SHUTDOWN
        assert not t.is_connected

    async def test_loop_assigns_session_id(self) -> None:
        t = MockTransport()
        sm = SessionManager(t, max_iterations=3)
        await sm.run_session_loop()
        assert sm.session_id is not None


class TestSessionShutdown:
    """shutdown_device() disconnects cleanly."""

    async def test_shutdown_disconnects(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        await sm.wait_for_device()
        await sm.shutdown_device()

        assert sm.state == SessionState.SHUTDOWN
        assert not t.is_connected

    async def test_shutdown_is_idempotent(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        await sm.wait_for_device()
        await sm.shutdown_device()
        await sm.shutdown_device()
        assert sm.state == SessionState.SHUTDOWN


class TestReceiveLoop:
    """Background receive loop processes messages."""

    async def test_receive_loop_processes_messages(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        events: list[str] = []
        sm.add_event_listener(lambda e, _d: events.append(e))

        await sm.wait_for_device()
        await sm.start_conversation()
        sm.start_receive_loop()

        for _ in range(50):
            if "device_status" in events or "audio_frame" in events:
                break
            await asyncio.sleep(0.01)

        await sm.stop_receive_loop()
        await sm.stop_conversation()

        assert "device_status" in events or "audio_frame" in events

    async def test_disconnect_cleanup_resets_handshake(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        await sm.wait_for_device()
        await sm.start_conversation()

        assert sm.handshake_complete
        assert sm._audio_bridge is not None
        sm._audio_bridge._schedule_unmute_timeout(3000)

        await sm._handle_device_disconnect("Connection lost while receiving: 1009")

        assert not sm.handshake_complete
        assert sm._audio_bridge is None
        assert sm.state == SessionState.CONNECTING
        assert sm._audio_bridge is None or sm._audio_bridge._unmute_timeout_task is None

    async def test_start_conversation_requires_handshake(self) -> None:
        t = MockTransport()
        sm = SessionManager(t)
        sm._state = SessionState.ACTIVE
        sm._handshake_complete = False

        with pytest.raises(TransportError, match="HELLO_ACK"):
            await sm.start_conversation()


class TestSessionFullLifecycle:
    """End-to-end: connect -> stream -> receive -> stop -> restart -> shutdown."""

    async def test_full_lifecycle(self) -> None:
        t = MockTransport()
        sm = SessionManager(t, max_iterations=5)

        await sm.wait_for_device()
        assert sm.state == SessionState.ACTIVE

        await sm.start_conversation()
        assert sm.state == SessionState.STREAMING

        msg = await t.receive_message()
        assert msg.type is not None

        await sm.stop_conversation()
        assert sm.state == SessionState.ACTIVE

        await sm.start_conversation()
        assert sm.state == SessionState.STREAMING

        await sm.stop_conversation()
        assert sm.state == SessionState.ACTIVE

        await sm.shutdown_device()
        assert sm.state == SessionState.SHUTDOWN
        assert not t.is_connected
