# Unit tests for weio_cli.cli._usage_next_reset and cmd_usage reset display.
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weio_cli import cli  # noqa: E402

NOW = datetime(2026, 11, 16, 17, 28, 52, tzinfo=timezone.utc)


@pytest.mark.parametrize("anchor,expected", [
    ("2026-11-16T00:00:00+00:00", "2026-11-17T00:00:00Z"),
    ("2026-11-06T00:00:00Z", "2026-11-17T00:00:00Z"),
    ("2026-11-16 00:00:00", "2026-11-17T00:00:00Z"),
    ("2026-11-16T01:00:00+01:00", "2026-11-17T00:00:00Z"),
    ("2026-11-17T00:00:00Z", "2026-11-17T00:00:00Z"),
])
def test_past_window_rolls_forward_and_future_boundary_preserved(anchor, expected):
    shown = cli._usage_next_reset(anchor, now=NOW)
    assert shown == expected
    assert datetime.fromisoformat(shown.replace("Z", "+00:00")) > NOW


def test_exact_boundary_rolls_to_following_day():
    shown = cli._usage_next_reset("2026-11-16T00:00:00Z", now=NOW.replace(hour=0, minute=0, second=0))
    assert shown == "2026-11-17T00:00:00Z"


@pytest.mark.parametrize("anchor", [None, 0, "", "not-a-time", "<invalid>"])
def test_invalid_timestamp_returns_none(anchor):
    assert cli._usage_next_reset(anchor, now=NOW) is None


def test_cmd_usage_shows_next_reset_and_preserves_quota(monkeypatch, capsys):
    pure = cli._usage_next_reset
    monkeypatch.setattr(cli, "_usage_next_reset", lambda v: pure(v, now=NOW))
    monkeypatch.setattr(cli, "_make_client", lambda args: SimpleNamespace(
        usage=lambda: {
            "email": "dev@example.test",
            "tier": "lite",
            "daily_tokens_used": 123,
            "daily_token_limit": 1000,
            "daily_tokens_remaining": 877,
            "reset_at": "2026-11-16T00:00:00Z",
        }
    ))
    assert cli.cmd_usage(SimpleNamespace()) == 0
    out = capsys.readouterr().out
    assert "2026-11-17T00:00:00Z" in out
    assert "2026-11-16T00:00:00Z" not in out
    assert "123 / 1,000" in out
    assert "877 tokens" in out


def test_cmd_usage_invalid_timestamp_shows_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_make_client", lambda args: SimpleNamespace(
        usage=lambda: {
            "email": "dev@example.test",
            "tier": "lite",
            "daily_tokens_used": 0,
            "daily_token_limit": 1000,
            "daily_tokens_remaining": 1000,
            "reset_at": "bad-ts",
        }
    ))
    assert cli.cmd_usage(SimpleNamespace()) == 0
    out = capsys.readouterr().out
    assert "unavailable (invalid reset timestamp)" in out


def test_cmd_usage_sentinel_displays_monthly_portal_link(monkeypatch, capsys):
    from weio_cli.client import WeioClient
    monkeypatch.setattr(cli, '_make_client', lambda args: SimpleNamespace(
        usage=lambda: {
            'email': 'solo@example.test',
            'tier': 'solo',
            'daily_tokens_used': 1000,
            'daily_token_limit': 2147483647,
            'daily_tokens_remaining': 2147482647,
        }
    ))
    res = cli.cmd_usage(SimpleNamespace(key=None, base=None, model=None))
    assert res == 0
    out = capsys.readouterr().out
    assert 'Monthly allowance: see Usage in Weio' in out
    assert '2,147,483,647' not in out
