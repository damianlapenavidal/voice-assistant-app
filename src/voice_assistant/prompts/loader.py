"""Load editable markdown prompt files bundled with this package.

Prompts are plain markdown so they can be edited without touching Python. Paths
are resolved relative to this package directory, so loading works regardless of
the current working directory.
"""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent

# In-memory cache of resolved prompt contents to avoid repeated disk reads.
_cache: dict[str, str] = {}


class PromptNotFoundError(FileNotFoundError):
    """Raised when a requested prompt file does not exist."""


def _read(relative_name: str) -> str:
    """Read and cache a prompt file by its name relative to the package dir.

    ``relative_name`` is given without the ``.md`` suffix, e.g. ``base_system``
    or ``personas/oso_animals``.
    """
    if relative_name in _cache:
        return _cache[relative_name]

    path = _PROMPTS_DIR / f"{relative_name}.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise PromptNotFoundError(
            f"Prompt file not found: {relative_name}.md (looked in {_PROMPTS_DIR})"
        ) from exc

    _cache[relative_name] = text
    return text


def load_prompt(name: str) -> str:
    """Load a single prompt file by name (without the ``.md`` suffix).

    Examples: ``load_prompt("base_system")``, ``load_prompt("opening_greeting")``,
    ``load_prompt("personas/oso_animals")``.
    """
    return _read(name)


def load_system_instructions(persona_id: str) -> str:
    """Compose full system instructions from the base prompt plus a persona.

    Final instructions = ``base_system.md`` + a blank line + the persona file at
    ``personas/{persona_id}.md``.
    """
    base = _read("base_system")
    persona = _read(f"personas/{persona_id}")
    return f"{base}\n\n{persona}"


def clear_cache() -> None:
    """Clear the in-memory prompt cache (mainly useful for tests)."""
    _cache.clear()
