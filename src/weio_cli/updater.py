"""Update checking + self-update.

A throttled, fail-silent check against PyPI (once per day) tells the user when a
newer weio-cli is published. `weio update` upgrades in place. Disable the check
with WEIO_NO_UPDATE_CHECK=1.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__

INDEX_JSON = os.environ.get("WEIO_UPDATE_INDEX", "https://pypi.org/pypi/weio-ai/json")
_CACHE_NAME = "update_check.json"
_TTL_HOURS = 24


def _cache_dir() -> Path:
    return Path(os.environ.get("WEIO_CONFIG_DIR", str(Path.home() / ".weio")))


def _parse_version(v: str) -> tuple:
    """Lenient numeric version tuple, e.g. '0.10.2' -> (0, 10, 2)."""
    out = []
    for part in (v or "").split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    return tuple(out) or (0,)


def is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def fetch_latest(timeout: float = 2.5) -> Optional[str]:
    try:
        import httpx
        r = httpx.get(INDEX_JSON, timeout=timeout,
                      headers={"User-Agent": f"weio-cli/{__version__}"})
        if r.status_code != 200:
            return None
        return r.json()["info"]["version"]
    except Exception:
        return None


def check_for_update(current: str = __version__, *, ttl_hours: int = _TTL_HOURS,
                     force: bool = False) -> Optional[str]:
    """Return the latest version string if newer than `current`, else None.

    Throttled to once per ttl_hours via a cache file; never raises.
    """
    if os.environ.get("WEIO_NO_UPDATE_CHECK") == "1":
        return None
    try:
        cache = _cache_dir() / _CACHE_NAME
        data = {}
        if cache.exists():
            try:
                data = json.loads(cache.read_text())
            except Exception:
                data = {}
        now = time.time()
        latest = data.get("latest")
        last = float(data.get("last_check", 0) or 0)

        if force or not latest or (now - last) > ttl_hours * 3600:
            fetched = fetch_latest()
            if fetched:
                latest = fetched
                try:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps({"last_check": now, "latest": latest}))
                except Exception:
                    pass

        if latest and is_newer(latest, current):
            return latest
    except Exception:
        pass
    return None


def update_notice(current: str = __version__) -> Optional[str]:
    """One-line, color-free notice if an update is available (for printing)."""
    latest = check_for_update(current)
    if not latest:
        return None
    return (f"weio-cli {latest} is available (you have {current}). "
            f"Update with: weio update")


def self_update(pre: bool = False) -> int:
    """Upgrade weio-cli.

    On Windows the running weio.exe is file-locked, so an in-place `pip install
    --upgrade` fails halfway and leaves a broken install. There we launch a
    DETACHED updater in a new window that waits for this process to exit, then runs
    pip — and we tell the caller to quit weio. Elsewhere (Linux/macOS) the in-place
    upgrade is safe.
    """
    py = sys.executable
    pip_args = "--upgrade --no-input weio-ai" + (" --pre" if pre else "")

    if os.name == "nt":
        # Wait 2s for weio to exit (unlocks weio.exe), upgrade, then keep the window
        # open so the user sees the result.
        # NOTE: do NOT pass the whole command line as a single ["cmd","/c", inner] arg —
        # subprocess.list2cmdline re-escapes the inner "{py}" quotes into '"C:\\...\\python.exe"',
        # which cmd rejects ("not recognized"), so the upgrade silently no-ops (bug repro'd on
        # Windows 2026-07-09). Write a .bat instead: quoting inside a batch file is literal, so
        # the python path stays correctly quoted and cmd just runs the file.
        import tempfile
        bat = (
            "@echo off\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            f'"{py}" -m pip install {pip_args}\r\n'
            "echo.\r\n"
            "echo Update finished - run weio again.\r\n"
            "pause\r\n"
        )
        try:
            fd, bat_path = tempfile.mkstemp(prefix="weio_update_", suffix=".bat")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(bat)
            CREATE_NEW_CONSOLE = 0x00000010
            subprocess.Popen(["cmd", "/c", bat_path], creationflags=CREATE_NEW_CONSOLE, close_fds=True)
            print("Updating in a new window. This weio session will now close so its "
                  "files can be replaced — reopen weio when the update finishes.")
            return 0
        except Exception as e:
            print(f"Couldn't launch the updater ({e}).", file=sys.stderr)
            print("Close all weio windows, then run:  pip install -U weio-ai", file=sys.stderr)
            return 1

    cmd = [py, "-m", "pip", "install", "--upgrade", "--no-input", "weio-ai"]
    if pre:
        cmd.append("--pre")
    print(f"Running: {' '.join(cmd)}")
    try:
        return subprocess.call(cmd)
    except Exception as e:
        print(f"Update failed: {e}", file=sys.stderr)
        print("Try manually:  pip install -U weio-ai", file=sys.stderr)
        return 1


def is_broken_install() -> bool:
    """True if a prior failed self-update left weio_cli partly uninstalled."""
    try:
        import weio_cli  # noqa: F401
        return False
    except Exception:
        return True
