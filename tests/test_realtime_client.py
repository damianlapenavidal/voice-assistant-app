"""Tests for OpenAI RealtimeClient with mocked WebSocket."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from voice_assistant.audio.utils import generate_test_tone, pcm16_to_base64
from voice_assistant.config import Config, DEFAULT_ASSISTANT_INSTRUCTIONS, DEFAULT_OPENAI_MODEL
from voice_assistant.openai_client.realtime import (
    RealtimeAudioDelta,
    RealtimeClient,
    RealtimeClientError,
    RealtimeErrorEvent,
    RealtimeNotConnectedError,
    RealtimeResponseDone,
    RealtimeSessionCreated,
    RealtimeSessionUpdated,
    RealtimeSpeechStarted,
    RealtimeSpeechStopped,
    RealtimeTranscript,
)


class MockWebSocket:
    """Minimal async WebSocket stand-in for RealtimeClient tests."""

    def __init__(self, incoming: asyncio.Queue[str | None]) -> None:
        self.sent: list[str] = []
        self._incoming = incoming
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    def __aiter__(self) -> AsyncIterator[str]:
        return self._iter_messages()

    async def _iter_messages(self) -> AsyncIterator[str]:
        while True:
            message = await self._incoming.get()
            if message is None:
                return
            yield message

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def incoming_queue() -> asyncio.Queue[str | None]:
    return asyncio.Queue()


@pytest.fixture
def mock_ws(incoming_queue: asyncio.Queue[str | None]) -> MockWebSocket:
    return MockWebSocket(incoming_queue)


async def _connect_client(
    mock_ws: MockWebSocket,
    incoming_queue: asyncio.Queue[str | None],
    **kwargs,
) -> RealtimeClient:
    async def fake_connect(url: str, additional_headers: dict[str, str] | None = None, **_kwargs):
        assert "api.openai.com/v1/realtime" in url
        assert additional_headers is not None
        assert additional_headers["Authorization"].startswith("Bearer ")
        return mock_ws

    client = RealtimeClient(
        api_key="test-key",
        connect_fn=fake_connect,
        **kwargs,
    )
    connect_task = asyncio.create_task(client.connect())
    await asyncio.sleep(0.01)
    await incoming_queue.put(json.dumps({"type": "session.updated", "session": {}}))
    await connect_task
    assert client.is_connected
    await _drain_session_updated(client)
    return client


async def _drain_session_updated(client: RealtimeClient) -> None:
    try:
        event = client._event_queue.get_nowait()
    except asyncio.QueueEmpty:
        return
    if not isinstance(event, RealtimeSessionUpdated):
        await client._event_queue.put(event)


class TestRealtimeClientConnect:
    async def test_connect_sends_session_update(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)

        assert len(mock_ws.sent) == 1
        session_update = json.loads(mock_ws.sent[0])
        assert session_update["type"] == "session.update"
        session = session_update["session"]
        assert session["model"] == DEFAULT_OPENAI_MODEL
        assert session["instructions"] == DEFAULT_ASSISTANT_INSTRUCTIONS
        assert session["audio"]["input"]["format"]["rate"] == 24000
        assert session["audio"]["input"]["turn_detection"]["type"] == "server_vad"
        vad = session["audio"]["input"]["turn_detection"]
        assert vad["create_response"] is True
        assert vad["interrupt_response"] is True
        assert 0.35 <= vad["threshold"] <= 0.85
        assert vad["silence_duration_ms"] >= 650
        assert vad["prefix_padding_ms"] == 300
        assert session["audio"]["output"]["voice"] == "alloy"

        await client.disconnect()

    async def test_connect_without_api_key_raises(self) -> None:
        client = RealtimeClient(api_key="")
        with pytest.raises(RealtimeClientError, match="OPENAI_API_KEY"):
            await client.connect()

    async def test_connect_twice_raises(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)
        with pytest.raises(RealtimeClientError, match="Already connected"):
            await client.connect()
        await client.disconnect()

    async def test_uses_config_defaults(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        config = Config(
            openai_api_key="cfg-key",
            openai_model="gpt-4o-realtime-preview",
            openai_voice="marin",
            assistant_instructions="Be helpful.",
        )

        async def fake_connect(url: str, additional_headers: dict[str, str] | None = None, **_kwargs):
            return mock_ws

        client = RealtimeClient(config=config, connect_fn=fake_connect)
        connect_task = asyncio.create_task(client.connect())
        await asyncio.sleep(0.01)
        await incoming_queue.put(json.dumps({"type": "session.updated", "session": {}}))
        await connect_task

        session = json.loads(mock_ws.sent[0])["session"]
        assert session["model"] == "gpt-4o-realtime-preview"
        assert session["instructions"] == "Be helpful."
        assert session["audio"]["output"]["voice"] == "marin"
        await client.disconnect()


class TestRealtimeClientSendAudio:
    async def test_send_audio_appends_base64_pcm(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)
        pcm = generate_test_tone(50)

        await client.send_audio(pcm)

        append_event = json.loads(mock_ws.sent[1])
        assert append_event["type"] == "input_audio_buffer.append"
        assert append_event["audio"] == pcm16_to_base64(pcm)
        await client.disconnect()

    async def test_send_audio_without_connection_raises(self) -> None:
        client = RealtimeClient(api_key="test-key")
        with pytest.raises(RealtimeNotConnectedError):
            await client.send_audio(b"\x00\x00")


class TestRealtimeClientBufferCommit:
    async def test_commit_input_buffer(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)
        await client.commit_input_buffer()

        commit_event = json.loads(mock_ws.sent[1])
        assert commit_event["type"] == "input_audio_buffer.commit"
        await client.disconnect()

    async def test_create_response(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)
        await client.create_response()

        create_event = json.loads(mock_ws.sent[1])
        assert create_event["type"] == "response.create"
        assert create_event["response"]["output_modalities"] == ["audio"]
        await client.disconnect()


class TestRealtimeClientOpeningGreeting:
    async def test_request_opening_greeting_uses_output_modalities(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        from voice_assistant.config import DEFAULT_OPENING_GREETING_INSTRUCTIONS

        client = await _connect_client(mock_ws, incoming_queue)
        await client.request_opening_greeting()

        greeting_event = json.loads(mock_ws.sent[1])
        assert greeting_event["type"] == "response.create"
        response = greeting_event["response"]
        assert response["output_modalities"] == ["audio"]
        assert "modalities" not in response
        assert response["instructions"] == DEFAULT_OPENING_GREETING_INSTRUCTIONS
        await client.disconnect()


class TestRealtimeClientEvents:
    async def test_session_created_event(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)
        await incoming_queue.put(
            json.dumps({"type": "session.created", "session": {"id": "sess_123"}}),
        )

        event = await asyncio.wait_for(client._event_queue.get(), timeout=1)
        assert isinstance(event, RealtimeSessionCreated)
        assert event.session_id == "sess_123"
        await client.disconnect()

    async def test_session_updated_event(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)
        await incoming_queue.put(
            json.dumps({"type": "session.updated", "session": {"model": "gpt-realtime-mini"}}),
        )

        event = await asyncio.wait_for(client._event_queue.get(), timeout=1)
        assert isinstance(event, RealtimeSessionUpdated)
        await client.disconnect()

    async def test_update_vad_settings_sends_session_update(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        from voice_assistant.audio.vad import derive_vad_settings

        client = await _connect_client(mock_ws, incoming_queue)
        settings = derive_vad_settings(noise_floor=300.0, user_speech_peak=900.0)

        update_task = asyncio.create_task(client.update_vad_settings(settings))
        await asyncio.sleep(0.01)
        await incoming_queue.put(json.dumps({"type": "session.updated", "session": {}}))
        await update_task

        session_updates = [
            json.loads(msg) for msg in mock_ws.sent if json.loads(msg)["type"] == "session.update"
        ]
        assert len(session_updates) == 2
        latest = session_updates[-1]["session"]["audio"]["input"]["turn_detection"]
        assert latest["threshold"] == settings.threshold
        await client.disconnect()

    async def test_output_audio_delta(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)
        pcm = generate_test_tone(20)
        encoded = pcm16_to_base64(pcm)

        await incoming_queue.put(
            json.dumps({"type": "response.output_audio.delta", "delta": encoded}),
        )

        event = await asyncio.wait_for(client._event_queue.get(), timeout=1)
        assert isinstance(event, RealtimeAudioDelta)
        assert event.pcm_bytes == pcm
        await client.disconnect()

    async def test_legacy_audio_delta_event(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)
        pcm = generate_test_tone(20)
        encoded = pcm16_to_base64(pcm)

        await incoming_queue.put(
            json.dumps({"type": "response.audio.delta", "delta": encoded}),
        )

        event = await asyncio.wait_for(client._event_queue.get(), timeout=1)
        assert isinstance(event, RealtimeAudioDelta)
        assert event.pcm_bytes == pcm
        await client.disconnect()

    async def test_response_done_emits_final_transcript(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)

        await incoming_queue.put(
            json.dumps(
                {"type": "response.audio_transcript.delta", "delta": "Hello there"},
            ),
        )
        delta_event = await asyncio.wait_for(client._event_queue.get(), timeout=1)
        assert isinstance(delta_event, RealtimeTranscript)
        assert delta_event.role == "assistant"
        assert delta_event.text == "Hello there"
        assert delta_event.final is False

        await incoming_queue.put(
            json.dumps({"type": "response.done", "response": {"id": "resp_1"}}),
        )
        final_event = await asyncio.wait_for(client._event_queue.get(), timeout=1)
        assert isinstance(final_event, RealtimeTranscript)
        assert final_event.final is True
        assert final_event.text == "Hello there"

        done_event = await asyncio.wait_for(client._event_queue.get(), timeout=1)
        assert isinstance(done_event, RealtimeResponseDone)
        assert done_event.response_id == "resp_1"
        await client.disconnect()

    async def test_speech_started_and_stopped_events(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)

        await incoming_queue.put(json.dumps({"type": "input_audio_buffer.speech_started"}))
        started = await asyncio.wait_for(client._event_queue.get(), timeout=1)
        assert isinstance(started, RealtimeSpeechStarted)

        await incoming_queue.put(json.dumps({"type": "input_audio_buffer.speech_stopped"}))
        stopped = await asyncio.wait_for(client._event_queue.get(), timeout=1)
        assert isinstance(stopped, RealtimeSpeechStopped)
        await client.disconnect()

    async def test_user_transcript_event(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)
        await incoming_queue.put(
            json.dumps(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "What is two plus two?",
                },
            ),
        )

        event = await asyncio.wait_for(client._event_queue.get(), timeout=1)
        assert isinstance(event, RealtimeTranscript)
        assert event.role == "user"
        assert event.text == "What is two plus two?"
        assert event.final is True
        await client.disconnect()

    async def test_error_event(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)
        await incoming_queue.put(
            json.dumps(
                {
                    "type": "error",
                    "error": {"message": "Invalid request", "code": "invalid_request"},
                },
            ),
        )

        event = await asyncio.wait_for(client._event_queue.get(), timeout=1)
        assert isinstance(event, RealtimeErrorEvent)
        assert event.message == "Invalid request"
        assert event.code == "invalid_request"
        await client.disconnect()

    async def test_on_event_callback(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        received: list[RealtimeAudioDelta] = []

        async def on_event(event) -> None:
            if isinstance(event, RealtimeAudioDelta):
                received.append(event)

        client = await _connect_client(mock_ws, incoming_queue, on_event=on_event)
        pcm = generate_test_tone(10)
        await incoming_queue.put(
            json.dumps(
                {
                    "type": "response.output_audio.delta",
                    "delta": pcm16_to_base64(pcm),
                },
            ),
        )
        await asyncio.wait_for(client._event_queue.get(), timeout=1)
        assert len(received) == 1
        assert received[0].pcm_bytes == pcm
        await client.disconnect()

    async def test_iter_events(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)
        await incoming_queue.put(
            json.dumps({"type": "session.created", "session": {"id": "sess_abc"}}),
        )

        async def collect_events():
            events = []
            async for event in client.iter_events():
                events.append(event)
                if isinstance(event, RealtimeSessionCreated):
                    break
            return events

        collected = await asyncio.wait_for(collect_events(), timeout=1)
        assert len(collected) == 1
        assert isinstance(collected[0], RealtimeSessionCreated)
        await client.disconnect()


class TestRealtimeClientDisconnect:
    async def test_disconnect_closes_websocket(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)
        await client.disconnect()
        assert mock_ws.closed
        assert not client.is_connected

    async def test_disconnect_is_idempotent(
        self,
        mock_ws: MockWebSocket,
        incoming_queue: asyncio.Queue[str | None],
    ) -> None:
        client = await _connect_client(mock_ws, incoming_queue)
        await client.disconnect()
        await client.disconnect()
        assert not client.is_connected
