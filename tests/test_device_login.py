"""Test exact Weio device protocol and trust failures with synthetic credentials."""

import json

import httpx
import pytest

from weio_cli import device_login as device

CODE = "weio_dc_" + "A" * 43
VALUE = "weio_sk_" + "B" * 43
LINK = "https://app.weio.ai/cli/device"
START = {
    "device_code": CODE, "user_code": "ABCD-2345", "verification_uri": LINK,
    "expires_in": 600, "interval": 5, "key_type": "cli_agent", "scopes": device.SCOPES,
}
APPROVED = {"status": "approved", "key": VALUE, "api_key": VALUE,
            "key_type": "cli_agent", "scopes": device.SCOPES}


def response(status, data, error=None):
    return httpx.Response(status, json={"success": status == 200, "data": data, "error": error})


@pytest.fixture
def clock(monkeypatch):
    now = [1.0]
    intervals = []
    monkeypatch.setattr(device.time, "monotonic", lambda: now[0])

    def sleep(delay):
        intervals.append(delay)
        now[0] += delay

    monkeypatch.setattr(device.time, "sleep", sleep)
    return intervals


def transport(monkeypatch, responses):
    original = httpx.Client
    requests = []
    options = []

    def handler(request):
        requests.append((str(request.url), json.loads(request.content)))
        return next(responses)

    def client(**params):
        options.append(params)
        return original(transport=httpx.MockTransport(handler), **params)

    monkeypatch.setattr(device.httpx, "Client", client)
    return requests, options


def test_device_wire_contract_and_no_token_output(monkeypatch, clock, capsys):
    responses = iter([response(200, START),
                      response(202, {"status": "authorization_pending"}, "authorization_pending"),
                      response(429, {"status": "slow_down", "interval": 10}, "slow_down"),
                      response(200, APPROVED)])
    requests, options = transport(monkeypatch, responses)
    opened = []
    monkeypatch.setattr(device.webbrowser, "open", opened.append)
    assert device.device_login(device.API_BASE, open_browser=True) == VALUE
    assert options == [{"follow_redirects": False, "trust_env": False}]
    assert requests[0] == ("https://app.weio.ai/api/public/cli/device/start", {"client_name": "weio-cli/1.0.5"})
    assert requests[1:] == [("https://app.weio.ai/api/public/cli/device/poll", {"device_code": CODE})] * 3
    assert clock == [5, 5, 10]
    assert opened == [LINK]
    outputs = capsys.readouterr()
    assert LINK in outputs.out and "ABCD-2345" in outputs.out
    assert CODE not in outputs.out + outputs.err and VALUE not in outputs.out + outputs.err


@pytest.mark.parametrize("base", ("https://evil.test/v1", "http://api.weio.ai/v1",
    "https://api.weio.ai.evil.test/v1", "https://api.weio.ai@evil.test/v1", "https://api.weio.ai:443/v1",
    "https://api.weio.ai/v1#fragment", "http://localhost:12345/v1", "http://127.0.0.1:12345/v1"))
def test_custom_host_never_gets_a_device_request(monkeypatch, base):
    monkeypatch.delenv("WEIO_DEVICE_TEST_LOOPBACK", raising=False)
    requests, options = transport(monkeypatch, iter([]))
    assert device.device_login(base) is None
    assert not requests and not options


def test_exact_test_only_loopback(monkeypatch):
    monkeypatch.setenv("WEIO_DEVICE_TEST_LOOPBACK", "1")
    assert device._origin("http://127.0.0.1:8081/v1") == "http://127.0.0.1:8081"
    assert device._origin("http://[::1]:8081/v1") == "http://[::1]:8081"
    for base in ("http://localhost:8081/v1", "http://127.0.0.2:8081/v1",
                 "http://x@127.0.0.1:8081/v1", "http://127.0.0.1:8081/v1?x",
                 "http://192.168.1.1:8081/v1", "http://127.0.0.1/v1"):
        assert device._origin(base) is None


@pytest.mark.parametrize("bad", ({"verification_uri": "https://evil.test/cli/device"},
    {"verification_uri": LINK + "?code=ABCD-2345"}, {"user_code": "bad\x1b[33m"},
    {"interval": -5}, {"interval": 400}, {"expires_in": 601}, {"device_code": "too-short"},
    {"key_type": "chat_only"}))
def test_untrusted_start_never_prints_the_code_or_opens_browser(monkeypatch, clock, capsys, bad):
    requests, _ = transport(monkeypatch, iter([response(200, START | bad)]))
    opened = []
    monkeypatch.setattr(device.webbrowser, "open", opened.append)
    assert device.device_login(device.API_BASE, open_browser=True) is None
    assert len(requests) == 1 and not opened
    outputs = capsys.readouterr()
    assert not outputs.out and "evil.test" not in outputs.err


@pytest.mark.parametrize("bad", ({"key": ["non-string"]}, {"key": "foreign-token"},
    {"key_type": "chat_only"}, {"scopes": ["chat"]}, {"scopes": ["*"]}, {"api_key": "contradiction"}))
def test_token_requires_exact_shape_and_scope(monkeypatch, clock, capsys, bad):
    transport(monkeypatch, iter([response(200, START), response(200, APPROVED | bad)]))
    assert device.device_login(device.API_BASE) is None
    outputs = capsys.readouterr()
    assert VALUE not in outputs.out + outputs.err
    assert "foreign-token" not in outputs.out + outputs.err


def test_legacy_prefilled_link_is_ignored_and_response_errors_are_sanitized(monkeypatch, clock, capsys):
    transport(monkeypatch, iter([
        response(200, START | {"verification_uri_complete": "https://evil.test/private"}),
        response(403, {"status": VALUE}, VALUE),
    ]))
    opened = []
    monkeypatch.setattr(device.webbrowser, "open", opened.append)
    assert device.device_login(device.API_BASE, open_browser=True) is None
    assert opened == [LINK]
    outputs = capsys.readouterr()
    assert "evil.test" not in outputs.out + outputs.err and VALUE not in outputs.out + outputs.err


def test_pending_cannot_poll_after_deadline(monkeypatch, clock, capsys):
    requests, _ = transport(monkeypatch, iter([response(200, START | {"expires_in": 8}),
            response(202, {"status": "authorization_pending"}, "authorization_pending")]))
    assert device.device_login(device.API_BASE) is None
    assert clock == [5, 3] and len(requests) == 2
    assert "timed out" in capsys.readouterr().err


def test_consumed_code_tells_user_to_revoke_unused_key(monkeypatch, clock, capsys):
    requests, _ = transport(monkeypatch, iter([
        response(200, START),
        response(410, {"status": "consumed", "unexpected": VALUE}, "invalid_grant"),
    ]))
    assert device.device_login(device.API_BASE) is None
    assert len(requests) == 2
    stdout, stderr = capsys.readouterr()
    assert "revoke the unused “Weio CLI (device code)” key in Settings → API keys" in stderr
    assert VALUE not in stdout + stderr and CODE not in stdout + stderr
