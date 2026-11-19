"""Terminal theme registry for the Weio CLI."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

_TRUECOLOR = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")

_WEIO_ORANGE = "38;2;226;85;53" if _TRUECOLOR else "38;5;203"
_TERMINAL_GREEN = "32"


@dataclass(frozen=True)
class Theme:
    name: str
    description: str
    ansi: Mapping[str, str]
    prompt_toolkit: Mapping[str, str]

    def code(self, role: str) -> str:
        return self.ansi.get(role, self.ansi.get("text", "0"))


DEFAULT_THEME = "weio"

THEMES: dict[str, Theme] = {
    "weio": Theme(
        name="weio",
        description="Light background with Weio orange accents",
        ansi={
            "text": "0",
            "dim": "2",
            "bold": "1",
            "brand": _WEIO_ORANGE,
            "accent": _WEIO_ORANGE,
            "prompt": _WEIO_ORANGE,
            "assistant": _WEIO_ORANGE,
            "tool": "34",
            "success": "32",
            "warning": "33",
            "error": "31",
            "diff_add": "32",
            "diff_remove": "31",
            "diff_hunk": _WEIO_ORANGE,
            "usage_ok": "32",
            "usage_warn": "33",
        },
        prompt_toolkit={
            "prompt": "#e25535 bold",
            "bottom-toolbar": "bg:#fff7ed #8a2a19",
        },
    ),
    "terminal": Theme(
        name="terminal",
        description="Dark terminal style with green accents",
        ansi={
            "text": "0",
            "dim": "2",
            "bold": "1",
            "brand": _TERMINAL_GREEN,
            "accent": _TERMINAL_GREEN,
            "prompt": _TERMINAL_GREEN,
            "assistant": _TERMINAL_GREEN,
            "tool": _TERMINAL_GREEN,
            "success": _TERMINAL_GREEN,
            "warning": "33",
            "error": "31",
            "diff_add": _TERMINAL_GREEN,
            "diff_remove": "31",
            "diff_hunk": "36",
            "usage_ok": _TERMINAL_GREEN,
            "usage_warn": "33",
        },
        prompt_toolkit={
            "prompt": "#33d17a bold",
            "bottom-toolbar": "bg:#111111 #33d17a",
        },
    ),
}

THEME_NAMES = tuple(THEMES)

_active_theme = THEMES[DEFAULT_THEME]


def get_theme(name: str | None) -> Theme:
    return THEMES.get((name or DEFAULT_THEME).lower(), THEMES[DEFAULT_THEME])


def normalize_theme(name: str | None) -> str:
    return get_theme(name).name


def set_active_theme(name: str | None) -> Theme:
    global _active_theme
    _active_theme = get_theme(name)
    return _active_theme


def active_theme() -> Theme:
    return _active_theme


def code(role: str, theme: Theme | None = None) -> str:
    return (theme or _active_theme).code(role)
