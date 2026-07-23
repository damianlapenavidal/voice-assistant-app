"""Editable markdown prompt files and their loader.

Assistant instructions live in the ``.md`` files in this package so they can be
edited without touching Python. Use :mod:`voice_assistant.prompts.loader` to
resolve them into strings.
"""

from voice_assistant.prompts.loader import (
    PromptNotFoundError,
    load_prompt,
    load_system_instructions,
)

__all__ = [
    "PromptNotFoundError",
    "load_prompt",
    "load_system_instructions",
]
