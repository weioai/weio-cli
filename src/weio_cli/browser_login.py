"""Browser-based login: open Weio in the browser, sign in (incl. Google), and
receive a freshly-minted API key on a local loopback server. No manual key paste.

Standard OAuth-CLI loopback pattern (like gcloud / gh): we start a localhost
server, open the browser to the Weio authorize page with our port + a random
state, the page mints a key and redirects back to the loopback with it.
"""
from __future__ import annotations

import http.server
import secrets
import socketserver
import sys
import time
import urllib.parse
import webbrowser
from typing import Optional


def _web_base_from_api(api_base: str) -> str:
    """Map public API base URLs (https://api.weio.ai/v1) to the app host (https://app.weio.ai)."""
    b = (api_base or "").rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    b = b.rstrip("/")
    parsed = urllib.parse.urlparse(b)
    if parsed.scheme in ("http", "https") and parsed.hostname in ("api.weio.ai", "weio.ai", "www.weio.ai") and not parsed.port:
        return f"{parsed.scheme}://app.weio.ai"
    return b


_DONE_HTML = (
    b"<!doctype html><html><head><meta charset='utf-8'><title>Weio CLI</title></head>"
    b"<body style='font-family:-apple-system,Segoe UI,sans-serif;background:#0d1117;"
    b"color:#e6edf3;display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
    b"<div style='text-align:center'><h2 style='color:#3fb950'>&#10003; Weio CLI authorized</h2>"
    b"<p>You can close this tab and return to your terminal.</p></div></body></html>"
)
_FAIL_HTML = (
    b"<!doctype html><html><body style='font-family:sans-serif'>"
    b"<h2>Authorization failed.</h2><p>Please run <code>weio login</code> again.</p></body></html>"
)


def browser_login(api_base: str, timeout: float = 180.0) -> Optional[str]:
    """Open the browser and return the minted API key, or None on failure/timeout."""
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            key = (params.get("key") or [None])[0]
            st = (params.get("state") or [None])[0]
            ok = bool(key) and st == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_DONE_HTML if ok else _FAIL_HTML)
            if ok:
                result["key"] = key

        def log_message(self, *args):  # silence
            pass

    try:
        httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    except OSError as e:
        print(f"Could not start local auth server: {e}", file=sys.stderr)
        return None
    httpd.timeout = 1
    port = httpd.server_address[1]
    auth_url = (f"{_web_base_from_api(api_base)}/cli/auth"
                f"?port={port}&state={urllib.parse.quote(state)}")

    print("Opening your browser to sign in to Weio…")
    print(f"  {auth_url}")
    print("  (If it doesn't open, copy that URL into your browser.)")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    deadline = time.time() + timeout
    try:
        while "key" not in result and time.time() < deadline:
            httpd.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return result.get("key")
