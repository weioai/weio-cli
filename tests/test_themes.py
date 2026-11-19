"""Tests for CLI theme settings and registry."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weio_cli import cli, settings, themes  # noqa: E402


def test_theme_registry_has_default_and_terminal():
    assert themes.get_theme("weio").name == "weio"
    assert themes.get_theme("terminal").name == "terminal"
    assert themes.normalize_theme("missing") == "weio"
    assert "prompt" in themes.get_theme("weio").ansi
    assert "bottom-toolbar" in themes.get_theme("terminal").prompt_toolkit


def test_settings_theme_roundtrip_and_unknown_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("WEIO_CONFIG_DIR", str(tmp_path))

    ok, msg = settings.set_value("theme", "terminal")
    assert ok
    assert msg == "theme = terminal"
    assert settings.load()["theme"] == "terminal"

    ok, msg = settings.set_value("theme", "not-real")
    assert ok
    assert "theme = weio" in msg
    assert settings.load()["theme"] == "weio"

    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["theme"] == "weio"


def test_config_set_theme_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WEIO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("WEIO_NO_UPDATE_CHECK", "1")

    rc = cli.main(["config", "set", "theme", "terminal"])

    assert rc == 0
    assert "theme = terminal" in capsys.readouterr().out
    assert settings.load()["theme"] == "terminal"
