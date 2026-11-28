import json

import pytest

from limitful import config


def test_settings_round_trip_and_environment_override(monkeypatch):
    assert config.load() == {} and config.provider() is None and config.api_key() is None
    config.update(provider="claude", api_key="  key-1  ")
    assert (config.provider(), config.api_key()) == ("claude", "key-1")
    config.update(provider=None)
    assert (config.provider(), config.api_key()) == (None, "key-1")
    monkeypatch.setenv("LIMITFUL_LLM", "ollama")
    assert config.provider() == "ollama"
    with pytest.raises(ValueError):
        config.update(provider="gpt")


def test_a_damaged_settings_file_is_treated_as_empty():
    config.config_path().parent.mkdir(parents=True)
    config.config_path().write_text("{not json")
    assert config.load() == {}
    config.config_path().write_text(json.dumps(["a list"]))
    assert config.load() == {}


def test_the_summary_model_can_be_chosen_and_defaults_to_opus(monkeypatch):
    assert config.claude_model() == "claude-opus-5-5"
    config.update(claude_model="claude-haiku-5-5")
    assert config.claude_model() == "claude-haiku-5-5"
    config.update(provider="claude")  # other changes keep the choice
    assert config.claude_model() == "claude-haiku-5-5"
    with pytest.raises(ValueError):
        config.update(claude_model="gpt-5")
    monkeypatch.setenv("LIMITFUL_CLAUDE_MODEL", "claude-sonnet-5-5")
    assert config.claude_model() == "claude-sonnet-5-5"
    monkeypatch.delenv("LIMITFUL_CLAUDE_MODEL")
    config.update(claude_model=None)
    assert config.claude_model() == "claude-opus-5-5"
