"""Tests for the ledger CLI command."""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weio_cli import cli  # noqa: E402


ROLLUP = {
    "success": True,
    "data": {
        "scope": "org",
        "totals": {"open": 3, "overdue": 1, "proposed": 2},
        "departments": [{"department": "Support", "open": 2, "overdue": 1}],
        "people": [{"name": "Grace Idunning", "department": "Support", "open": 1, "overdue": 0}],
    },
}

ENTRIES = {
    "success": True,
    "data": {
        "scope": "org",
        "entries": [
            {
                "id": "task:team:1",
                "kind": "task",
                "title": "Duplicate charge review",
                "state": "open",
                "priority": "high",
                "assignee_name": "Grace Idunning",
                "department": "Support",
                "due_date": "2026-07-10",
                "created_at": "2026-07-06T10:00:00",
                "origin": "team_tasks",
            }
        ],
    },
}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def _mock_get(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        if url.endswith("/api/public/ledger/rollup"):
            return FakeResponse(ROLLUP)
        if "/api/public/ledger?" in url:
            return FakeResponse(ENTRIES)
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("httpx.get", fake_get)
    return calls


def test_ledger_default_rollup_and_open_entries(monkeypatch, capsys):
    monkeypatch.setenv("WEIO_API_KEY", "weio_sk_test")
    monkeypatch.setenv("WEIO_BASE", "https://example.test/v1")
    monkeypatch.setenv("WEIO_NO_UPDATE_CHECK", "1")
    calls = _mock_get(monkeypatch)

    rc = cli.main(["ledger"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Ledger (org)" in out
    assert "Open: 3  Overdue: 1  Proposed: 2" in out
    assert "Departments" in out and "Support" in out
    assert "People" in out and "Grace Idunning" in out
    assert "Open entries" in out
    assert "Duplicate charge review" in out
    assert calls[0]["headers"]["Authorization"] == "Bearer weio_sk_test"
    assert calls[0]["url"] == "https://example.test/api/public/ledger/rollup"
    parsed = urlparse(calls[1]["url"])
    assert parsed.path == "/api/public/ledger"
    params = parse_qs(parsed.query)
    assert params["state"] == ["open"]
    assert params["limit"] == ["15"]


def test_ledger_all_requests_100_without_default_state(monkeypatch, capsys):
    monkeypatch.setenv("WEIO_API_KEY", "weio_sk_test")
    monkeypatch.setenv("WEIO_BASE", "https://example.test/v1")
    monkeypatch.setenv("WEIO_NO_UPDATE_CHECK", "1")
    calls = _mock_get(monkeypatch)

    rc = cli.main(["ledger", "--all"])

    assert rc == 0
    assert "Entries" in capsys.readouterr().out
    params = parse_qs(urlparse(calls[1]["url"]).query, keep_blank_values=True)
    assert params["state"] == [""]
    assert params["limit"] == ["100"]


def test_ledger_filters_are_passed_to_entries(monkeypatch, capsys):
    monkeypatch.setenv("WEIO_API_KEY", "weio_sk_test")
    monkeypatch.setenv("WEIO_BASE", "https://example.test/v1")
    monkeypatch.setenv("WEIO_NO_UPDATE_CHECK", "1")
    calls = _mock_get(monkeypatch)

    rc = cli.main(["ledger", "--person", "Grace", "--state", "proposed", "--kind", "task"])

    assert rc == 0
    assert "Duplicate charge review" in capsys.readouterr().out
    params = parse_qs(urlparse(calls[1]["url"]).query)
    assert params["person"] == ["Grace"]
    assert params["state"] == ["proposed"]
    assert params["kind"] == ["task"]
    assert params["limit"] == ["15"]
