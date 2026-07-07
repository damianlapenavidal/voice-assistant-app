"""Dedicated web dashboard server thread (isolated from the device event loop)."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from voice_assistant.web.app import DashboardManager

log = structlog.get_logger()


def start_web_server_thread(
    dashboard: DashboardManager,
    port: int,
    *,
    ready: threading.Event,
) -> threading.Thread:
    """Run uvicorn on its own event loop so it does not conflict with device websockets."""

    def _run() -> None:
        import uvicorn

        from voice_assistant.web.app import create_app

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        dashboard.set_web_loop(loop)

        app = create_app(dashboard)
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="warning",
        )
        server = uvicorn.Server(config)

        async def _serve() -> None:
            await server.serve()

        ready.set()
        log.info("web.dashboard_starting", port=port)
        try:
            loop.run_until_complete(_serve())
        finally:
            loop.close()

    thread = threading.Thread(target=_run, name="web-dashboard", daemon=True)
    thread.start()
    return thread
