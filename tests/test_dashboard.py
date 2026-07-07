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

        dashboard = DashboardManager(session)
        main_loop = asyncio.get_running_loop()

        result = await dashboard.handle_browser_command("start_session")

        assert result["status"] == "ok"
        assert session.state == SessionState.STREAMING
        assert asyncio.get_running_loop() is main_loop

        await session.stop_conversation()

    async def test_session_event_broadcast_schedules_on_same_loop(self) -> None:
        transport = MockTransport()
        session = SessionManager(transport)
        dashboard = DashboardManager(session)
        main_loop = asyncio.get_running_loop()

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

        dashboard = DashboardManager(session)
        main_loop = asyncio.get_running_loop()

        result = await dashboard.handle_browser_command("stop_session")

        assert result["status"] == "ok"
        assert session.state == SessionState.ACTIVE
        assert asyncio.get_running_loop() is main_loop
