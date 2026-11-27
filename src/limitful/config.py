"""User settings, kept outside the data folder.

The data folder is something people back up and sync; an API key does not
belong in it. Settings live in ~/.config/limitful/config.json, readable only
by the user.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

PROVIDERS = ("claude", "ollama")
TRANSCRIBERS = ("local", "deepgram")  # local Whisper, or a hosted service with speaker labels


def config_path() -> Path:
    return Path(os.environ.get("LIMITFUL_CONFIG")
                or Path.home() / ".config" / "limitful" / "config.json")


def load() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(settings: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".tmp")
    # Create the file private from the start, so the key is never world-readable.
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(settings, out, indent=2)
    os.chmod(partial, 0o600)
    os.replace(partial, path)


_KEEP: Any = object()  # "leave this setting as it is"
_updating = threading.Lock()


def update(provider: Any = _KEEP, api_key: Any = _KEEP, led_brightness: Any = _KEEP,
           transcriber: Any = _KEEP, deepgram_api_key: Any = _KEEP,
           voice_memos: Any = _KEEP, voice_memos_since: Any = _KEEP) -> dict:
    """Change the given settings and return the result. `None` clears a setting."""
    with _updating:  # two requests saving at once must not undo each other
        settings = _update(provider, api_key, led_brightness, transcriber, deepgram_api_key)
        if voice_memos is not _KEEP or voice_memos_since is not _KEEP:
            if voice_memos is not _KEEP:
                settings["voice_memos"] = bool(voice_memos)
            if voice_memos_since is not _KEEP:
                settings["voice_memos_since"] = int(voice_memos_since or 0)
            save(settings)
        return settings


def _update(provider: Any, api_key: Any, led_brightness: Any, transcriber: Any, deepgram_api_key: Any) -> dict:
    settings = load()
    if provider is not _KEEP:
        if provider is not None and provider not in PROVIDERS:
            raise ValueError(f"provider must be one of {', '.join(PROVIDERS)}, or empty for none")
        settings["provider"] = provider
    if api_key is not _KEEP:
        key = (api_key or "").strip()
        if key:
            settings["anthropic_api_key"] = key
        else:
            settings.pop("anthropic_api_key", None)
    if transcriber is not _KEEP:
        if transcriber not in TRANSCRIBERS:
            raise ValueError(f"transcription must be one of {', '.join(TRANSCRIBERS)}")
        settings["transcriber"] = transcriber
    if deepgram_api_key is not _KEEP:
        key = (deepgram_api_key or "").strip()
        if key:
            settings["deepgram_api_key"] = key
        else:
            settings.pop("deepgram_api_key", None)
    if led_brightness is not _KEEP:
        settings["led_brightness"] = led_brightness  # the pendant cannot be asked, so remember it
    save(settings)
    return settings


def provider() -> str | None:
    """What writes summaries: the environment wins over the saved setting."""
    chosen = os.environ.get("LIMITFUL_LLM") or load().get("provider")
    return chosen if chosen in PROVIDERS else None


def api_key() -> str | None:
    return load().get("anthropic_api_key") or None


def transcriber() -> str:
    """What turns audio into text: local Whisper unless the user chose a hosted service."""
    chosen = os.environ.get("LIMITFUL_TRANSCRIBER") or load().get("transcriber")
    return chosen if chosen in TRANSCRIBERS else "local"


def voice_memos() -> tuple[bool, int]:
    """Whether Voice Memos on this Mac are brought in, and from what moment on (ms)."""
    settings = load()
    since = settings.get("voice_memos_since")
    return bool(settings.get("voice_memos")), since if isinstance(since, int) else 0


def deepgram_api_key() -> str | None:
    return load().get("deepgram_api_key") or None
