"""weio-cli 1.0: native tool-calling client plumbing (ops #129).

Covers the gateway capability probe (cached, defaults False on any error) and
chat_with_tools() passing tools/tool_choice through and parsing tool_calls back
out of the response. All HTTP is mocked -- nothing here hits a real gateway.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weio_cli.client import WeioClient, WeioError  # noqa: E402


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text or str(self._payload)

    def json(self):
        return self._payload


def _client() -> WeioClient:
    return WeioClient("weio_sk_test", "https://example.test/v1")


# ---- probe_tool_calling ----

def test_probe_true_when_gateway_advertises_it(monkeypatch):
    monkeypatch.setattr("httpx.get", lambda url, headers=None, timeout=None: FakeResponse({"tool_calling": True}))
    c = _client()
    assert c.probe_tool_calling() is True


def test_probe_false_on_older_gateway_without_the_field(monkeypatch):
    monkeypatch.setattr("httpx.get", lambda url, headers=None, timeout=None: FakeResponse({"service": "old gateway"}))
    c = _client()
    assert c.probe_tool_calling() is False


def test_probe_false_on_network_error(monkeypatch):
    import httpx as real_httpx

    def boom(url, headers=None, timeout=None):
        raise real_httpx.ConnectError("down")

    monkeypatch.setattr("httpx.get", boom)
    c = _client()
    assert c.probe_tool_calling() is False


def test_probe_false_on_non_200(monkeypatch):
    monkeypatch.setattr("httpx.get", lambda url, headers=None, timeout=None: FakeResponse({}, status_code=500))
    c = _client()
    assert c.probe_tool_calling() is False


def test_probe_result_is_cached_after_first_call(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        return FakeResponse({"tool_calling": True})

    monkeypatch.setattr("httpx.get", fake_get)
    c = _client()
    assert c.probe_tool_calling() is True
    assert c.probe_tool_calling() is True
    assert calls["n"] == 1


# ---- chat_with_tools ----

def test_chat_with_tools_sends_tools_and_tool_choice(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return FakeResponse({
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 12},
        })

    monkeypatch.setattr("httpx.post", fake_post)
    c = _client()
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    result = c.chat_with_tools([{"role": "user", "content": "hi"}], tools=tools, tool_choice="auto")
    assert captured["payload"]["tools"] == tools
    assert captured["payload"]["tool_choice"] == "auto"
    assert result == {"content": "ok", "tool_calls": None, "finish_reason": "stop"}
    assert c.session_tokens == 12


def test_chat_with_tools_parses_tool_calls_and_null_content(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return FakeResponse({
            "choices": [{
                "message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}},
                ]},
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        })

    monkeypatch.setattr("httpx.post", fake_post)
    c = _client()
    result = c.chat_with_tools([{"role": "user", "content": "read a.py"}])
    assert result["content"] is None
    assert result["finish_reason"] == "tool_calls"
    assert result["tool_calls"][0]["function"]["name"] == "read_file"


def test_chat_with_tools_401_raises_weio_error(monkeypatch):
    monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResponse({}, status_code=401))
    c = _client()
    try:
        c.chat_with_tools([{"role": "user", "content": "hi"}])
        assert False, "expected WeioError"
    except WeioError as e:
        assert "401" in str(e) or "Authentication" in str(e)
