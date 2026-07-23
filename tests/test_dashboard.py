"""Tests for the web dashboard sharing the main asyncio event loop."""

import asyncio

from voice_assistant.core.session import SessionManager, SessionState
from voice_assistant.transport.mock_transport import MockTransport
from voice_assistant.web.app import DashboardManager


class TestDashboardEventLoop:
    """Dashboard commands and broadcasts must run on the same loop as SessionManager."""

    async def test_start_session_runs_on_same_loop(self) -> None:
        transport = MockTransport()
        session = SessionManager(transport)
        await session.wait_for_device()

        main_loop = asyncio.get_running_loop()
        dashboard = DashboardManager(session, main_loop=main_loop)
        dashboard.set_web_loop(main_loop)

        result = await dashboard.handle_browser_command("start_session")

        assert result["status"] == "ok"
        assert session.state == SessionState.STREAMING
        assert asyncio.get_running_loop() is main_loop

        await session.stop_conversation()

    async def test_set_volume_sends_set_volume_message(self) -> None:
        from voice_assistant.core.message import MessageType

        transport = MockTransport()
        session = SessionManager(transport)
        await session.wait_for_device()

        main_loop = asyncio.get_running_loop()
        dashboard = DashboardManager(session, main_loop=main_loop)
        dashboard.set_web_loop(main_loop)

        result = await dashboard.handle_browser_command(
            "set_volume", {"action": "set_volume", "value": 70},
        )

        assert result["status"] == "ok"
        volume_msgs = [m for m in transport.sent_messages if m.type == MessageType.SET_VOLUME]
        assert len(volume_msgs) == 1
        assert volume_msgs[0].payload == {"volume": 70}

    async def test_set_volume_rejects_invalid_value(self) -> None:
        transport = MockTransport()
        session = SessionManager(transport)
        await session.wait_for_device()

        main_loop = asyncio.get_running_loop()
        dashboard = DashboardManager(session, main_loop=main_loop)
        dashboard.set_web_loop(main_loop)

        result = await dashboard.handle_browser_command(
            "set_volume", {"action": "set_volume", "value": "not-a-number"},
        )

        assert result["status"] == "error"

    async def test_session_event_broadcast_schedules_on_same_loop(self) -> None:
        transport = MockTransport()
        session = SessionManager(transport)
        main_loop = asyncio.get_running_loop()
        dashboard = DashboardManager(session, main_loop=main_loop)
        dashboard.set_web_loop(main_loop)

        await session.wait_for_device()
        await asyncio.sleep(0)

        assert dashboard._browser_clients == []
        assert any(
            entry["event"] == "session_started"
            for entry in dashboard._message_log
        )
        assert asyncio.get_running_loop() is main_loop

    async def test_stop_session_runs_on_same_loop(self) -> None:
        transport = MockTransport()
        session = SessionManager(transport)
        await session.wait_for_device()
        await session.start_conversation()

        main_loop = asyncio.get_running_loop()
        dashboard = DashboardManager(session, main_loop=main_loop)
        dashboard.set_web_loop(main_loop)

        result = await dashboard.handle_browser_command("stop_session")

        assert result["status"] == "ok"
        assert session.state == SessionState.ACTIVE
        assert asyncio.get_running_loop() is main_loop

    async def test_pause_then_resume_session(self) -> None:
        transport = MockTransport()
        session = SessionManager(transport)
        await session.wait_for_device()
        await session.start_conversation()

        main_loop = asyncio.get_running_loop()
        dashboard = DashboardManager(session, main_loop=main_loop)
        dashboard.set_web_loop(main_loop)

        pause_result = await dashboard.handle_browser_command("pause_session")
        assert pause_result["status"] == "ok"
        assert session.state == SessionState.PAUSED

        resume_result = await dashboard.handle_browser_command("resume_session")
        assert resume_result["status"] == "ok"
        assert session.state == SessionState.STREAMING

        await session.stop_conversation()

    async def test_turn_on_after_shutdown(self) -> None:
        transport = MockTransport()
        session = SessionManager(transport)
        await session.wait_for_device()
        await session.shutdown_device()

        main_loop = asyncio.get_running_loop()
        dashboard = DashboardManager(session, main_loop=main_loop)
        dashboard.set_web_loop(main_loop)

        result = await dashboard.handle_browser_command("turn_on")

        assert result["status"] == "ok"
        # MockTransport completes its fake handshake near-instantly, so by the
        # time the cross-thread command future resolves the state may already
        # have advanced past CONNECTING to ACTIVE -- either is a valid sign
        # that turn_on() re-armed the device server.
        assert session.state in (SessionState.CONNECTING, SessionState.ACTIVE)

        await session.stop_receive_loop()

    async def test_history_stores_only_final_transcripts(self) -> None:
        transport = MockTransport()
        session = SessionManager(transport)
        await session.wait_for_device()
        dashboard = DashboardManager(session, main_loop=asyncio.get_running_loop())

        # Streaming partials must not be persisted in the bounded history...
        dashboard._on_session_event(
            "transcript", {"role": "assistant", "text": "Hel", "final": False}
        )
        dashboard._on_session_event(
            "transcript", {"role": "assistant", "text": "Hello", "final": False}
        )
        assert len(dashboard._transcripts) == 0

        # ...only the completed line is kept.
        dashboard._on_session_event(
            "transcript", {"role": "assistant", "text": "Hello there!", "final": True}
        )
        assert list(dashboard._transcripts) == [
            {
                "role": "assistant",
                "text": "Hello there!",
                "final": True,
                "timestamp": dashboard._transcripts[0]["timestamp"],
            }
        ]

    async def test_stop_clears_transcripts_but_pause_does_not(self) -> None:
        transport = MockTransport()
        session = SessionManager(transport)
        await session.wait_for_device()
        await session.start_conversation()

        main_loop = asyncio.get_running_loop()
        dashboard = DashboardManager(session, main_loop=main_loop)
        dashboard.set_web_loop(main_loop)

        dashboard._transcripts.append({"role": "user", "text": "hi", "final": True})

        await dashboard.handle_browser_command("pause_session")
        assert len(dashboard._transcripts) == 1

        await dashboard.handle_browser_command("resume_session")
        await dashboard.handle_browser_command("stop_session")
        assert len(dashboard._transcripts) == 0
