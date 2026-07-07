# Voice Assistant App

The "brain" of a Raspberry Pi voice assistant for kids. This application runs on a laptop (and eventually a phone) and orchestrates communication between a Raspberry Pi device and the OpenAI Realtime API.

## System Overview

This project is part of a three-repo system that builds a kid-friendly voice assistant:

- **This repo** (`voice-assistant-app`) -- The application layer that manages sessions, handles the OpenAI Realtime API connection, and enforces parent controls.
- **[voice-assistant-pi5](https://github.com/damianlapenavidal/voice-assistant-pi5)** -- Audio capture and playback on the Raspberry Pi 5.
- **[voice-assistant-piZero2W](https://github.com/damianlapenavidal/voice-assistant-piZero2W)** -- Audio capture and playback on the Raspberry Pi Zero 2 W.

The Pi devices are intentionally "dumb" -- they capture microphone audio, send it to this app, and play back audio they receive. All intelligence, API keys, and session management live here in the app layer.

## Architecture

The system is organized into three layers:

```
Device Layer (Raspberry Pi)
  --> captures mic audio, plays speaker audio

App Layer (this repo)
  --> receives audio from device, manages sessions,
      connects to OpenAI, sends responses back to device

AI Layer (OpenAI Realtime API)
  --> provides conversational AI via WebSocket streaming
```

Communication between the device and app uses a JSON-based protocol over WebSocket, with audio encoded as base64 PCM16 at 24kHz mono -- the same format the OpenAI Realtime API expects, so no conversion is needed.

## Quick Start

```bash
# Clone the repository
git clone https://github.com/damianlapenavidal/voice-assistant-app.git
cd voice-assistant-app

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy the environment template and fill in your API key
cp .env.example .env

# Run in mock mode (no hardware or API key required)
python -m voice_assistant --mock
```

## Project Status

**Phase 0 -- Planning and Setup**

The project is in its initial setup phase. The folder structure, dependencies, and architecture documentation are being established. See the [phased roadmap](docs/roadmap.md) for the full development plan.

## Folder Structure

```
src/voice_assistant/
  main.py                    # CLI entrypoint
  config.py                  # Configuration loading (.env, defaults)
  core/
    session.py               # Session manager (device + AI orchestration)
    message.py               # Protocol message types (Pydantic models)
  transport/
    base.py                  # Abstract transport interface
    websocket_transport.py   # WebSocket implementation (Wi-Fi)
    mock_transport.py        # Mock transport for testing without hardware
  openai_client/
    realtime.py              # OpenAI Realtime API WebSocket client
  audio/
    utils.py                 # Audio format helpers (PCM, base64, resampling)
tests/                       # Unit and integration tests
docs/                        # Architecture docs, protocol spec, roadmap
```

## Documentation

See the [docs/](docs/) directory for detailed documentation:

- Architecture design and diagrams
- Device-app protocol specification
- Phased development roadmap
