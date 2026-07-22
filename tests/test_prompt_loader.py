"""Tests for the markdown prompt loader and config integration."""

from __future__ import annotations

import pytest

from voice_assistant.config import load_config
from voice_assistant.prompts import (
    PromptNotFoundError,
    load_prompt,
    load_system_instructions,
)
from voice_assistant.prompts.loader import clear_cache


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    clear_cache()


@pytest.fixture
def isolated_env(monkeypatch):
    """Isolate load_config() from the developer's local .env file."""
    import voice_assistant.config as config_module

    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)
    for var in ("ASSISTANT_INSTRUCTIONS", "PERSONA", "OPENING_GREETING_PROMPT"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


class TestLoadPrompt:
    def test_loads_bundled_prompts(self) -> None:
        assert load_prompt("base_system").strip()
        assert load_prompt("opening_greeting").strip()
        assert load_prompt("personas/oso_animals").strip()
        assert load_prompt("personas/chef_coco_food").strip()
        assert load_prompt("personas/robi_colors").strip()

    def test_strips_surrounding_whitespace(self) -> None:
        text = load_prompt("opening_greeting")
        assert text == text.strip()

    def test_missing_prompt_raises(self) -> None:
        with pytest.raises(PromptNotFoundError):
            load_prompt("does_not_exist")


class TestLoadSystemInstructions:
    def test_composes_base_and_persona(self) -> None:
        base = load_prompt("base_system")
        persona = load_prompt("personas/oso_animals")
        composed = load_system_instructions("oso_animals")

        assert base in composed
        assert persona in composed
        # Base comes first, persona second, separated by a blank line.
        assert composed == f"{base}\n\n{persona}"

    def test_different_personas_differ(self) -> None:
        oso = load_system_instructions("oso_animals")
        coco = load_system_instructions("chef_coco_food")
        assert oso != coco
        assert "Chef Coco" in coco

    def test_missing_persona_raises(self) -> None:
        with pytest.raises(PromptNotFoundError):
            load_system_instructions("no_such_persona")


class TestConfigIntegration:
    def test_default_config_uses_composed_instructions(self, isolated_env) -> None:
        config = load_config()
        assert config.persona_id == "oso_animals"
        assert config.assistant_instructions == load_system_instructions("oso_animals")
        assert config.opening_greeting_instructions == load_prompt("opening_greeting")

    def test_persona_env_selects_persona(self, isolated_env) -> None:
        isolated_env.setenv("PERSONA", "chef_coco_food")

        config = load_config()
        assert config.persona_id == "chef_coco_food"
        assert config.assistant_instructions == load_system_instructions("chef_coco_food")

    def test_assistant_instructions_override_bypasses_files(self, isolated_env) -> None:
        isolated_env.setenv("ASSISTANT_INSTRUCTIONS", "Custom inline prompt.")

        config = load_config()
        assert config.assistant_instructions == "Custom inline prompt."
