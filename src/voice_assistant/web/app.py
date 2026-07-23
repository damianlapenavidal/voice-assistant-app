"""FastAPI web dashboard for controlling voice assistant sessions."""

import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from voice_assistant.core.session import SessionManager, SessionState

if TYPE_CHECKING:
    from fastapi import FastAPI, WebSocket

log = structlog.get_logger()

STATIC_DIR = Path(__file__).parent / "static"


class DashboardManager:
    """Bridges SessionManager events to browser WebSocket clients."""

    def __init__(
        self,
        session_manager: SessionManager,
        *,
        main_loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._session_manager = session_manager
        self._main_loop = main_loop
        self._web_loop: asyncio.AbstractEventLoop | None = None
        self._browser_clients: list["WebSocket"] = []
        self._message_log: deque[dict[str, Any]] = deque(maxlen=100)
        self._device_info: dict[str, Any] = {}
        self._device_status: dict[str, Any] = {}
        self._transcripts: deque[dict[str, Any]] = deque(maxlen=50)

        self._mic_muted = False
        session_manager.add_event_listener(self._on_session_event)

    def set_web_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the dashboard asyncio loop (runs in the web server thread)."""
        self._web_loop = loop

    def _on_session_event(self, event: str, data: dict[str, Any]) -> None:
        """Handle events from SessionManager (called on the main event loop)."""
        if event == "session_started":
            self._device_info = data.get("device_info") or {}
        elif event == "device_status":
            self._device_status = data
        elif event == "device_disconnected":
            self._device_info = {}
            self._device_status = {}
        elif event == "session_shutdown":
            self._device_info = {}
            self._device_status = {}
        elif event == "conversation_stopped":
            self._mic_muted = False
            self._transcripts.clear()
        elif event == "mic_muted":
            self._mic_muted = data.get("muted", False)
        elif event == "transcript":
            # Persist only completed lines in the bounded history. Streaming
            # partials are still broadcast live to connected browsers below, but
            # keeping them here would let a single long reply's many deltas evict
            # earlier final lines from the deque.
            if data.get("final"):
                self._transcripts.append({
                    "role": data.get("role", ""),
                    "text": data.get("text", ""),
                    "final": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

        entry = {
            "event": event,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._message_log.append(entry)
        self._schedule_broadcast(entry)

    def _schedule_broadcast(self, entry: dict[str, Any]) -> None:
        """Forward a broadcast to the web server thread."""
        if self._web_loop is None or self._web_loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(entry), self._web_loop)
        except RuntimeError:
            log.warning(
                "dashboard.broadcast_skipped_no_loop",
                event=entry.get("event"),
            )

    async def _broadcast(self, data: dict[str, Any]) -> None:
        """Send data to all connected browser clients."""
        payload = json.dumps(data)
        disconnected: list["WebSocket"] = []
        for ws in self._browser_clients:
            try:
                await ws.send_text(payload)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            self._browser_clients.remove(ws)

    async def register_browser(self, ws: "WebSocket") -> None:
        await ws.accept()
        self._browser_clients.append(ws)
        log.info("dashboard.browser_connected", total=len(self._browser_clients))

        await ws.send_text(json.dumps(self._get_full_state()))

    def unregister_browser(self, ws: "WebSocket") -> None:
        if ws in self._browser_clients:
            self._browser_clients.remove(ws)
        log.info("dashboard.browser_disconnected", total=len(self._browser_clients))

    def _get_full_state(self) -> dict[str, Any]:
        sm = self._session_manager
        transport = sm.transport
        return {
            "event": "full_state",
            "data": {
                "session_state": sm.state.name,
                "session_id": sm.session_id,
                "handshake_complete": sm.handshake_complete,
                "device_connected": transport.is_connected,
                "device_info": self._device_info,
                "device_status": self._device_status,
                "api_key_configured": sm.is_openai_configured,
                "audio_mode": sm.active_mode,
                "conversation_state": sm.conversation_state,
                "mic_muted": self._mic_muted,
                "transcripts": list(self._transcripts),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def handle_browser_command(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Execute a command from the browser dashboard (web thread -> main loop)."""
        future = asyncio.run_coroutine_threadsafe(
            self._handle_browser_command_main(action, payload or {}),
            self._main_loop,
        )
        return await asyncio.wrap_future(future)

    async def _handle_browser_command_main(
        self,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, str]:
        """Run dashboard commands on the main session event loop."""
        sm = self._session_manager
        try:
            match action:
                case "start_session":
                    if not sm.handshake_complete:
                        return {
                            "status": "error",
                            "message": "Waiting for device handshake (HELLO_ACK)",
                        }
                    if sm.state == SessionState.ACTIVE:
                        await sm.start_conversation()
                        return {"status": "ok", "message": "Conversation started"}
                    return {"status": "error", "message": f"Cannot start: state is {sm.state.name}"}
                case "pause_session":
                    if sm.state == SessionState.STREAMING:
                        await sm.pause_conversation()
                        return {"status": "ok", "message": "Conversation paused"}
                    return {"status": "error", "message": f"Cannot pause: state is {sm.state.name}"}
                case "resume_session":
                    if sm.state == SessionState.PAUSED:
                        await sm.resume_conversation()
                        return {"status": "ok", "message": "Conversation resumed"}
                    return {"status": "error", "message": f"Cannot resume: state is {sm.state.name}"}
                case "stop_session":
                    if sm.state in (SessionState.STREAMING, SessionState.PAUSED):
                        await sm.stop_conversation()
                        return {"status": "ok", "message": "Conversation stopped"}
                    return {"status": "error", "message": f"Cannot stop: state is {sm.state.name}"}
                case "shutdown":
                    await sm.shutdown_device()
                    return {"status": "ok", "message": "Device shutdown sent"}
                case "set_volume":
                    if not sm.handshake_complete:
                        return {
                            "status": "error",
                            "message": "Waiting for device handshake (HELLO_ACK)",
                        }
                    try:
                        volume = int(payload.get("value"))
                    except (TypeError, ValueError):
                        return {"status": "error", "message": "Invalid volume value"}
                    await sm.set_volume(volume)
                    return {"status": "ok", "message": f"Volume set to {volume}%"}
                case "set_mic_gain":
                    if not sm.handshake_complete:
                        return {
                            "status": "error",
                            "message": "Waiting for device handshake (HELLO_ACK)",
                        }
                    try:
                        gain = int(payload.get("value"))
                    except (TypeError, ValueError):
                        return {"status": "error", "message": "Invalid mic gain value"}
                    await sm.set_mic_gain(gain)
                    return {"status": "ok", "message": f"Mic gain set to {gain}%"}
                case "turn_on":
                    if sm.state == SessionState.SHUTDOWN:
                        await sm.turn_on()
                        return {
                            "status": "ok",
                            "message": (
                                "Waiting for device to reconnect "
                                "(remote power-on is not automated yet)"
                            ),
                        }
                    return {"status": "error", "message": f"Cannot turn on: state is {sm.state.name}"}
                case _:
                    return {"status": "error", "message": f"Unknown action: {action}"}
        except Exception as exc:
            log.error("dashboard.command_error", action=action, error=str(exc))
            return {"status": "error", "message": str(exc)}


def create_app(dashboard: DashboardManager) -> "FastAPI":
    """Create the FastAPI dashboard app."""
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse

    app = FastAPI(title="Voice Assistant Dashboard")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html_path = STATIC_DIR / "index.html"
        return HTMLResponse(content=html_path.read_text())

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await dashboard.register_browser(ws)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    cmd = json.loads(raw)
                    action = cmd.get("action", "")
                    result = await dashboard.handle_browser_command(action, cmd)
                    await ws.send_text(json.dumps({
                        "event": "command_result",
                        "data": result,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }))
                except json.JSONDecodeError:
                    await ws.send_text(json.dumps({
                        "event": "command_result",
                        "data": {"status": "error", "message": "Invalid JSON"},
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }))
        except WebSocketDisconnect:
            pass
        finally:
            dashboard.unregister_browser(ws)

    return app
