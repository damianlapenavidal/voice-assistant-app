"""CLI entrypoint for the voice assistant."""

from __future__ import annotations

import argparse
import asyncio
import socket
import threading
import time
from functools import partial
from typing import Any

import structlog

from voice_assistant.config import Config, configure_logging, load_config

log = structlog.get_logger()
_BOOT_MONO: float | None = None


def _elapsed_s() -> float:
    if _BOOT_MONO is None:
        return 0.0
    return round(__import__("time").monotonic() - _BOOT_MONO, 1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Voice Assistant App — brain of a Raspberry Pi voice assistant",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run in mock mode with a simulated device",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="Number of mock iterations (default: from config or 20)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="WebSocket server host (default: from config or 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="WebSocket server port (default: from config or 8765)",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Start the web dashboard alongside the device server",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=None,
        help="Web dashboard port (default: from config or 8080)",
    )
    parser.add_argument(
        "--target",
        type=str,
        default=None,
        help=(
            "Raspberry Pi target this session is for (default: from "
            "VOICE_ASSISTANT_TARGET). Rejects a handshake from a different board."
        ),
    )
    parser.add_argument(
        "--loopback",
        action="store_true",
        help="Force loopback mode even when an OpenAI API key is configured",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override log level",
    )
    return parser.parse_args(argv)


def _warm_web_stack() -> None:
    """Import uvicorn/FastAPI off the event loop (cold start can take minutes)."""
    import uvicorn  # noqa: F401

    import fastapi  # noqa: F401


def _warn_if_slow(
    future: asyncio.Future[Any],
    event: str,
    *,
    after_sec: float = 5.0,
) -> None:
    """Log a warning if a background future is still pending after a delay.

    Makes an otherwise-silent slow import (e.g. macOS scanning freshly
    installed compiled packages on first run) visible in the logs instead of
    looking like a hang.
    """

    async def _watch() -> None:
        await asyncio.sleep(after_sec)
        if not future.done():
            log.warning(event, elapsed_s=_elapsed_s())

    asyncio.create_task(_watch(), name=f"watchdog-{event}")


def _wait_for_port(port: int, *, timeout_sec: float = 10.0) -> bool:
    """Return True once something is listening on localhost:port."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.1)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def _build_session(
    config: Config,
    *,
    use_loopback: bool,
) -> tuple[object, bool]:
    """Import core modules and construct SessionManager (slow on cold start)."""
    from voice_assistant.core.session import SessionManager

    if config.mock_mode:
        from voice_assistant.transport.mock_transport import MockTransport

        transport = MockTransport()
        session = SessionManager(
            transport,
            max_iterations=config.max_mock_iterations,
            loopback=use_loopback,
            config=config,
        )
        return session, False

    from voice_assistant.transport.websocket_transport import WebSocketTransport

    transport = WebSocketTransport(host=config.device_host, port=config.device_port)
    session = SessionManager(
        transport,
        max_iterations=0,
        loopback=use_loopback,
        config=config,
    )
    return session, True


async def _start_web_dashboard(
    session: object,
    web_port: int,
    *,
    web_warm_future: asyncio.Future[Any] | None = None,
) -> None:
    """Start the dashboard on a dedicated thread (avoids WS conflicts on the main loop).

    Waits for the (potentially slow) fastapi/uvicorn import to finish, but this
    never blocks the device server — that path does not depend on this future.
    """
    if web_warm_future is not None:
        await web_warm_future

    from voice_assistant.web.app import DashboardManager
    from voice_assistant.web.server import start_web_server_thread

    main_loop = asyncio.get_running_loop()
    dashboard = DashboardManager(session, main_loop=main_loop)
    ready = threading.Event()
    start_web_server_thread(dashboard, web_port, ready=ready)

    if not await asyncio.to_thread(ready.wait, 120):
        log.warning("web.dashboard_thread_timeout", port=web_port)
    if not await asyncio.to_thread(_wait_for_port, web_port, timeout_sec=10.0):
        log.warning("web.dashboard_bind_timeout", port=web_port)

    log.info("web.dashboard_started", port=web_port, url=f"http://localhost:{web_port}")


async def _start_device_server(session: object) -> None:
    """Bind the device WebSocket server and run handshake/messages in the background."""
    await session.start_device_server()
    session.start_receive_loop()


async def _run_app(
    session: object,
    *,
    device_mode: bool,
    web_enabled: bool,
    web_port: int,
    web_warm_future: asyncio.Future[Any] | None = None,
) -> None:
    """Run the device server on the main loop; dashboard runs on its own thread.

    The device server never waits on the dashboard (or its slow imports) --
    a Pi can connect immediately even while the web stack is still loading.
    """
    startup: list[asyncio.Task[Any]] = []

    if device_mode:
        startup.append(asyncio.create_task(_start_device_server(session), name="device-startup"))
    if web_enabled:
        startup.append(
            asyncio.create_task(
                _start_web_dashboard(session, web_port, web_warm_future=web_warm_future),
                name="web-startup",
            ),
        )

    if startup:
        await asyncio.gather(*startup)

    try:
        if device_mode:
            await asyncio.Event().wait()
        else:
            await session.run_session_loop()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("app.interrupted")
    finally:
        if device_mode:
            await session.stop_receive_loop()
            await session.shutdown_device()


async def _main_async(
    config: Config,
    *,
    use_loopback: bool,
    force_loopback: bool,
) -> None:
    """Load heavy modules in parallel, then start services."""
    log.info(
        "app.starting",
        mode="mock" if config.mock_mode else "device",
        target=config.target or None,
        host=config.device_host,
        port=config.device_port,
        web=config.web_enabled,
        audio_mode="loopback" if use_loopback else "openai",
        iterations=config.max_mock_iterations if config.mock_mode else None,
    )

    loop = asyncio.get_running_loop()

    # Kick off the (potentially slow, e.g. first-run OS security scanning of
    # newly installed packages) fastapi/uvicorn import in the background, but
    # do NOT gate the device server on it -- a Pi should be able to connect
    # the moment the (fast) session build finishes, regardless of how long
    # the web stack takes to warm up.
    web_warm_future: asyncio.Future[Any] | None = None
    if config.web_enabled:
        log.info("web.loading_dependencies")
        web_warm_future = loop.run_in_executor(None, _warm_web_stack)
        _warn_if_slow(web_warm_future, "web.dependencies_still_loading")

    log.info("app.loading_modules", elapsed_s=_elapsed_s())
    session, device_mode = await loop.run_in_executor(
        None,
        partial(_build_session, config, use_loopback=use_loopback),
    )
    log.info("app.modules_ready", elapsed_s=_elapsed_s())

    if config.openai_api_key and not force_loopback and not use_loopback:
        log.info("app.openai_mode", model=config.openai_model, voice=config.openai_voice)
    elif not config.mock_mode:
        reason = "forced" if force_loopback else "no API key"
        log.info("app.loopback_mode", reason=reason)

    await _run_app(
        session,
        device_mode=device_mode,
        web_enabled=config.web_enabled,
        web_port=config.web_port,
        web_warm_future=web_warm_future,
    )


def main(argv: list[str] | None = None) -> None:
    global _BOOT_MONO
    _BOOT_MONO = __import__("time").monotonic()

    args = parse_args(argv)
    config = load_config()

    if args.mock:
        config.mock_mode = True
    if args.iterations is not None:
        config.max_mock_iterations = args.iterations
    if args.host is not None:
        config.device_host = args.host
    if args.port is not None:
        config.device_port = args.port
    if args.web:
        config.web_enabled = True
    if args.web_port is not None:
        config.web_port = args.web_port
    if args.target is not None:
        config.target = args.target
    if args.log_level:
        config.log_level = args.log_level

    configure_logging(config.log_level)
    log.info(
        "app.booting",
        mode="mock" if config.mock_mode else "device",
        web=config.web_enabled,
        elapsed_s=_elapsed_s(),
    )

    force_loopback = args.loopback
    use_loopback = force_loopback or not config.openai_api_key

    try:
        asyncio.run(
            _main_async(
                config,
                use_loopback=use_loopback,
                force_loopback=force_loopback,
            )
        )
    except KeyboardInterrupt:
        log.info("app.interrupted")


if __name__ == "__main__":
    main()
