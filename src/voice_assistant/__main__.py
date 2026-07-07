"""Allow running the package via `python -m voice_assistant`."""

import sys

print("voice-assistant: launching...", file=sys.stderr, flush=True)

from voice_assistant.main import main

main()
