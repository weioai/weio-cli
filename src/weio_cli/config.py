"""Configuration: API key + base URL resolution and persistence.

Resolution order (first hit wins):
  1. Explicit --key / --base CLI flags
  2. WEIO_API_KEY / WEIO_BASE environment variables
  3. ~/.weio/config.json  (written by `weio login`)
  4. Built-in default base

~/.weio/config.json holds two kinds of fields:
  - auth fields: api_key, key_source, account_email, account_tier — written by
    `weio login`, cleared by `weio logout`.
  - everything else (currently just `base`) — a general CLI preference that
    `weio logout` leaves alone.

`key_source` records how the active on-disk key was obtained ("browser" for the
loopback browser sign-in, "api_key" for a pasted/--api-key dashboard key) so
`weio whoami` can report a key type without the backend needing to distinguish
them itself (both are plain rows in `public_api_keys`, same `weio_sk_` prefix).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

DEFAULT_BASE = "https://api.weio.ai/v1"

# Fields cleared by `weio logout`; everything else in config.json is preserved.
AUTH_FIELDS = ("api_key", "key_source", "account_email", "account_tier")


def _dir() -> Path:
    return Path(os.environ.get("WEIO_CONFIG_DIR", str(Path.home() / ".weio")))


def _file() -> Path:
    return _dir() / "config.json"


def _read_file() -> dict:
    try:
        return json.loads(_file().read_text())
    except Exception:
        return {}


def _write_file(data: dict) -> Path:
    _dir().mkdir(parents=True, exist_ok=True)
    path = _file()
    path.write_text(json.dumps(data, indent=2))
    try:
        path.chmod(0o600)
    except Exception:
        pass
    return path


def save(key: Optional[str] = None, base: Optional[str] = None, *,
         source: Optional[str] = None, account_email: Optional[str] = None,
         account_tier: Optional[str] = None) -> Path:
    """Persist key/base (+ optional identity cache) to ~/.weio/config.json (0600)."""
    data = _read_file()
    if key is not None:
        data["api_key"] = key
    if base is not None:
        data["base"] = base
    if source is not None:
        data["key_source"] = source
    if account_email is not None:
        data["account_email"] = account_email
    if account_tier is not None:
        data["account_tier"] = account_tier
    return _write_file(data)


def clear_auth() -> dict:
    """Remove only the auth fields from config.json; keep everything else (e.g. base).

    Returns the identity that was cleared: {"had_key": bool, "email": str|None,
    "source": str|None}. Idempotent — calling this with no stored key just
    returns had_key=False and leaves the file untouched.
    """
    data = _read_file()
    had_key = bool(data.get("api_key"))
    removed = {"had_key": had_key, "email": data.get("account_email"),
              "source": data.get("key_source")}
    if not had_key:
        return removed
    for field in AUTH_FIELDS:
        data.pop(field, None)
    _write_file(data)
    return removed


def current_identity() -> dict:
    """Best-effort, no-network snapshot of the on-disk credential: {"email",
    "tier", "source"} (any may be None, e.g. an older CLI version's key that
    predates identity caching, or no stored key at all)."""
    data = _read_file()
    return {
        "email": data.get("account_email"),
        "tier": data.get("account_tier"),
        "source": data.get("key_source"),
    }


def resolve_key(cli_key: Optional[str]) -> Optional[str]:
    return cli_key or os.environ.get("WEIO_API_KEY") or _read_file().get("api_key")


def resolve_key_source(cli_key: Optional[str] = None) -> str:
    """Best-effort classification of where the ACTIVE credential came from, for
    `weio whoami`. One of: 'flag', 'env', 'browser', 'api_key', 'unknown'.

    The server can't tell these apart (every credential is a plain row in
    public_api_keys with the same weio_sk_ prefix, whether minted by the browser
    loopback flow or issued from the dashboard) — this is CLI-local bookkeeping.
    """
    if cli_key:
        return "flag"
    if os.environ.get("WEIO_API_KEY"):
        return "env"
    data = _read_file()
    if data.get("api_key"):
        return data.get("key_source") or "unknown"
    return "unknown"


def resolve_base(cli_base: Optional[str]) -> str:
    return (
        cli_base
        or os.environ.get("WEIO_BASE")
        or _read_file().get("base")
        or DEFAULT_BASE
    )
