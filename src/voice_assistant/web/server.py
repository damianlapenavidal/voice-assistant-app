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
            # Signal readiness only once uvicorn has actually started: the
            # socket is bound AND app startup has completed, so the very first
            # request a caller makes after `ready` will be served. Setting the
            # event before serve() (as this used to) let callers -- and the
            # "web.dashboard_started" log -- race ahead of a server that could
            # not yet answer HTTP, so the dashboard "wasn't loading yet".
            async def _signal_ready_when_started() -> None:
                try:
                    while not server.started:
                        await asyncio.sleep(0.02)
                finally:
                    # Set even if serve() aborts, so callers fall through to
                    # their timeout instead of blocking the full 120s.
                    ready.set()

            ready_task = asyncio.ensure_future(_signal_ready_when_started())
            log.info("web.dashboard_starting", port=port)
            try:
                await server.serve()
            finally:
                ready_task.cancel()
                ready.set()

        try:
            loop.run_until_complete(_serve())
        finally:
            loop.close()

    thread = threading.Thread(target=_run, name="web-dashboard", daemon=True)
    thread.start()
    return thread
