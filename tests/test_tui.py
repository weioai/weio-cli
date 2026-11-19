"""Tests for unit-testable TUI input helpers."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weio_cli import tui  # noqa: E402


class FakeBuffer:
    def __init__(self):
        self.text = ""
        self.submitted = False

    def insert_text(self, text):
        self.text += text

    def validate_and_handle(self):
        self.submitted = True


class FakeEvent:
    def __init__(self):
        self.current_buffer = FakeBuffer()


def test_alt_enter_handler_inserts_newline_without_submitting():
    event = FakeEvent()

    tui._insert_newline(event)

    assert event.current_buffer.text == "\n"
    assert event.current_buffer.submitted is False


def test_enter_handler_submits_without_mutating_text():
    event = FakeEvent()
    event.current_buffer.text = "line one\nline two"

    tui._submit_input(event)

    assert event.current_buffer.text == "line one\nline two"
    assert event.current_buffer.submitted is True
