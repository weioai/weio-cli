"""Device-code sign-in through the fixed Weio app origin."""

from __future__ import annotations

import math
import os
import re
import sys
import time
from typing import Optional
from urllib.parse import urlsplit
import webbrowser

import httpx

from . import __version__

API_BASE = "https://api.weio.ai/v1"
APP_ORIGIN = "https://app.weio.ai"
DEVICE_CODE = re.compile(r"weio_dc_[A-Za-z0-9_-]{43}")
USER_CODE = re.compile(r"[2-9ABCDEFGHJKLMNPQRSTUVWXYZ]{4}-[2-9ABCDEFGHJKLMNPQRSTUVWXYZ]{4}")
DEVICE_KEY = re.compile(r"weio_sk_[A-Za-z0-9_-]{43}")
SCOPES = ["chat", "documents", "agents"]
MIN_INTERVAL = 5
MAX_INTERVAL = 30
MAX_TTL = 600


def _origin(api_base: str) -> Optional[str]:
    if api_base in (API_BASE, API_BASE + "/"):
        return APP_ORIGIN
    # Offline end-to-end fixtures can explicitly opt in to a numeric loopback
    # with an exact port. No custom external gateway can mint a device key.
    if os.environ.get("WEIO_DEVICE_TEST_LOOPBACK") != "1":
        return None
    try:
        url = urlsplit(api_base)
        if (url.scheme == "http" and url.hostname in ("127.0.0.1", "::1")
                and url.port and url.path in ("/v1", "/v1/") and not url.query and not url.fragment):
            host = "127.0.0.1" if url.hostname == "127.0.0.1" else "[::1]"
            authority = f"{host}:{url.port}"
            if url.netloc == authority and api_base == f"http://{authority}{url.path}":
                return f"http://{authority}"
    except ValueError:
        pass
    return None


def _response(http: httpx.Client, endpoint: str, data: dict, timeout: float) -> tuple[int, dict]:
    response = http.post(endpoint, json=data, timeout=timeout)
    if len(response.content) > 16384:
        raise ValueError("unexpected device response size")
    result = response.json()
    if type(result) is not dict or type(result.get("data")) is not dict:
        raise ValueError("unexpected device response shape")
    return response.status_code, result


def _match(pattern, value) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _valid_number(value, low, high) -> bool:
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def _scoped(data) -> bool:
    return data.get("key_type") == "cli_agent" and data.get("scopes") == SCOPES


def _exchange(http: httpx.Client, origin: str, timeout: float, open_browser: bool) -> Optional[str]:
    endpoint = origin + "/api/public/cli/device"
    link = origin + "/cli/device"
    deadline = time.monotonic() + min(timeout, MAX_TTL)
    status, body = _response(
        http, endpoint + "/start", {"client_name": f"weio-cli/{__version__}"}, min(15, timeout),
    )
    data = body["data"]
    device_code = data.get("device_code")
    user_code = data.get("user_code")
    ttl = data.get("expires_in")
    interval = data.get("interval")
    if (status != 200 or body.get("success") is not True or not _scoped(data)
            or not _match(DEVICE_CODE, device_code) or not _match(USER_CODE, user_code)
            or data.get("verification_uri") != link
            or not _valid_number(ttl, 1, MAX_TTL) or not _valid_number(interval, MIN_INTERVAL, MAX_INTERVAL)):
        raise ValueError("invalid device sign-in response")

    print(f"Open {link} in any signed-in browser.")
    print(f"Enter code: {user_code}", flush=True)
    if open_browser:
        try:
            webbrowser.open(link)
        except Exception:
            pass

    deadline = min(deadline, time.monotonic() + ttl)
    while time.monotonic() < deadline:
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            status, body = _response(
                http, endpoint + "/poll", {"device_code": device_code}, min(15, remaining),
            )
        except httpx.TransportError:
            continue
        if time.monotonic() >= deadline:
            break
        data = body["data"]
        error = body.get("error")
        if status == 200 and body.get("success") is True and data.get("status") == "approved":
            key = data.get("key")
            if (not _scoped(data) or not _match(DEVICE_KEY, key)
                    or ("api_key" in data and data["api_key"] != key)):
                raise ValueError("invalid device credential response")
            return key
        if status == 202 and error == "authorization_pending":
            continue
        if status == 429 and error in ("slow_down", "rate_limited"):
            updated = data.get("interval")
            interval = min(MAX_INTERVAL, max(interval + MIN_INTERVAL, updated)) if _valid_number(
                updated, MIN_INTERVAL, MAX_INTERVAL
            ) else min(MAX_INTERVAL, interval + MIN_INTERVAL)
            continue
        if status == 410:
            print("This device code was already used. If sign-in did not complete, revoke the unused "
                  "“Weio CLI (device code)” key in Settings → API keys before trying again.",
                  file=sys.stderr)
        elif error == "expired_token" or data.get("status") == "expired":
            print("The device code expired. Run `weio login` to start again.", file=sys.stderr)
        else:
            print("Device sign-in was refused. Run `weio login` to start again.", file=sys.stderr)
        return None

    print("Device sign-in timed out before approval.", file=sys.stderr)
    return None


def device_login(api_base: str, timeout: float = 600.0, open_browser: bool = False) -> Optional[str]:
    origin = _origin(api_base)
    if origin is None:
        print("Device sign-in requires the default Weio gateway. For a custom gateway, paste its API key with `weio login --no-browser`.",
              file=sys.stderr)
        return None
    if not _valid_number(timeout, 0.05, MAX_TTL):
        return None
    try:
        with httpx.Client(follow_redirects=False, trust_env=False) as http:
            return _exchange(http, origin, timeout, open_browser)
    except (httpx.HTTPError, ValueError):
        print("Device sign-in could not be verified. Run `weio login` to try again.", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nDevice sign-in cancelled.", file=sys.stderr)
    return None
