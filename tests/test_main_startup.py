"""Tests for CLI startup bootstrap."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from voice_assistant.config import Config
from voice_assistant.main import _main_async, _run_app


@pytest.mark.asyncio
async def test_main_async_loads_modules_in_parallel() -> None:
    config = Config(mock_mode=True, web_enabled=True, max_mock_iterations=1)
    session = object()
    load_order: list[str] = []

    async def fake_run_app(*args: object, **kwargs: object) -> None:
        load_order.append("run_app")

    def fake_build_session(*args: object, **kwargs: object) -> tuple[object, bool]:
        load_order.append("session")
        return session, False

    def fake_warm_web_stack() -> None:
        load_order.append("web")

    with (
        patch("voice_assistant.main._build_session", fake_build_session),
        patch("voice_assistant.main._warm_web_stack", fake_warm_web_stack),
        patch("voice_assistant.main._run_app", fake_run_app),
    ):
        await _main_async(config, use_loopback=True, force_loopback=False)

    assert set(load_order[:2]) == {"session", "web"}
    assert load_order[-1] == "run_app"


@pytest.mark.asyncio
async def test_run_app_starts_web_server() -> None:
    session = AsyncMock()
    session.run_session_loop = AsyncMock()
    order: list[str] = []

    async def web_start(_session: object, _port: int) -> asyncio.Task[None]:
        order.append("web_start")

        async def noop() -> None:
            return None

        return asyncio.create_task(noop())

    with patch("voice_assistant.main._start_web_server", web_start):
        run_task = asyncio.create_task(
            _run_app(
                session,
                device_mode=False,
                web_enabled=True,
                web_port=8080,
            )
        )
        await asyncio.sleep(0.01)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    assert order == ["web_start"]
