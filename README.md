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

## Running against a Raspberry Pi

One command per target, from the repo root on the Mac:

```bash
./scripts/setup-target-config.sh    # one time: creates config/targets.local.env

./scripts/start-pi5.sh              # Raspberry Pi 5
./scripts/start-pizero2w.sh         # Raspberry Pi Zero 2 W

./scripts/terminate-pi5.sh          # stop Pi service + Mac app + tunnels
./scripts/terminate-pizero2w.sh
```

Each launcher checks SSH before touching Wi-Fi, switches the Mac to the target's
saved network only when needed, updates the Pi with `git pull --ff-only` (never
destructively), runs a silent ALSA preflight, restarts the endpoint service, waits
for it to be ready, and then starts this app with that target selected.

`Ctrl+C` in the start terminal stops the Mac app (and tunnel) but leaves the Pi
service running. Use `terminate-*` to tear down the full session.

```bash
./scripts/start-pizero2w.sh --dry-run   # show every step, change nothing
./scripts/start-pizero2w.sh --logs      # follow the endpoint journal
```

Audio hardware can be checked separately. `--info` and `--mic` are silent;
`--speaker` and `--loopback` make sound and ask first:

```bash
./scripts/audio-diagnostic.sh pizero2w --info
./scripts/audio-diagnostic.sh pizero2w --mic
```

See **[docs/raspberry-pi-development-workflow.md](docs/raspberry-pi-development-workflow.md)**
for setup, the verified Pi Zero 2 W audio configuration, and troubleshooting.

## Project Status

Working Mac + Raspberry Pi voice assistant with OpenAI Realtime, bilingual
persona prompts, parent web dashboard, and Mac-side deploy/terminate scripts
for the Pi 5 and Pi Zero 2 W.

## Folder Structure

```
scripts/                     # Mac-side launch + deploy workflow
  start-pi5.sh               # wrapper -> start-target.sh pi5
  start-pizero2w.sh          # wrapper -> start-target.sh pizero2w
  start-target.sh            # the shared launch implementation
  terminate-pi5.sh           # wrapper -> stop-target.sh pi5
  terminate-pizero2w.sh      # wrapper -> stop-target.sh pizero2w
  stop-target.sh             # stop Pi service, Mac app, orphan tunnels
  setup-target-config.sh     # creates config/targets.local.env, lists SSIDs
  audio-diagnostic.sh        # explicit, opt-in mic/speaker tests
  lib/                       # common, config, network, ssh, deploy, audio,
                             #   service, app, tunnel
config/
  targets.example.env        # committed, secret-free target template
deploy/systemd/              # endpoint service templates (installed on a Pi)
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
docs/                        # Architecture, protocol, Pi workflow, learning design
```

## Documentation

See the [docs/](docs/) directory for detailed documentation:

- [Architecture](docs/architecture.md) — system layers and design
- [Protocol](docs/protocol.md) — device ↔ app message contract
- [Raspberry Pi workflow](docs/raspberry-pi-development-workflow.md) — launch, deploy, audio
- [Learning design](docs/learning-design.md) — bilingual teaching pedagogy and prompts
