"""Persistent CLI settings (~/.weio/settings.json).

Separate from config.py (which holds the API key/base). These are behavior
preferences for the coding agent.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import themes

APPROVAL_MODES = ("suggest", "auto-edit", "full-auto")

DEFAULTS = {
    "model": "auto",            # auto | low | mid | pro
    "approval": "suggest",      # suggest | auto-edit | full-auto
    "theme": themes.DEFAULT_THEME,
    "max_steps": 25,            # max agent tool-steps per task
    "max_tokens": 4096,
    "stream": True,
}

_VALID_KEYS = set(DEFAULTS)


def _dir() -> Path:
    return Path(os.environ.get("WEIO_CONFIG_DIR", str(Path.home() / ".weio")))


def _file() -> Path:
    return _dir() / "settings.json"


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        if _file().exists():
            data.update({k: v for k, v in json.loads(_file().read_text()).items() if k in _VALID_KEYS})
    except Exception:
        pass
    data["theme"] = themes.normalize_theme(data.get("theme"))
    return data


def save(data: dict) -> Path:
    _dir().mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in data.items() if k in _VALID_KEYS}
    clean["theme"] = themes.normalize_theme(clean.get("theme"))
    _file().write_text(json.dumps(clean, indent=2))
    return _file()


def set_value(key: str, value: str) -> tuple[bool, str]:
    """Set one setting from a string value. Returns (ok, message)."""
    if key not in _VALID_KEYS:
        return False, f"Unknown setting '{key}'. Valid: {', '.join(sorted(_VALID_KEYS))}"
    data = load()
    if key == "approval":
        if value not in APPROVAL_MODES:
            return False, f"approval must be one of: {', '.join(APPROVAL_MODES)}"
        data[key] = value
    elif key == "theme":
        requested = (value or "").lower()
        resolved = themes.normalize_theme(requested)
        data[key] = resolved
        save(data)
        if requested and requested != resolved:
            return True, f"theme = {resolved} (unknown '{value}', using default)"
        return True, f"theme = {resolved}"
    elif key in ("max_steps", "max_tokens"):
        try:
            data[key] = max(1, int(value))
        except ValueError:
            return False, f"{key} must be an integer"
    elif key == "stream":
        data[key] = value.lower() in ("1", "true", "yes", "on")
    else:  # model
        data[key] = value
    save(data)
    return True, f"{key} = {data[key]}"
