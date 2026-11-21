# Unit tests for weio-cli 1.0.3 r6 claims-only README parity and /workspace/keys URL.
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from weio_cli import cli  # noqa: E402

BARE_KEYS_URL_RE = re.compile(r"https://app\.weio\.ai/keys(?![/\w-])")


def test_r6_readme_uses_workspace_keys_and_excludes_bare_404_keys_url():
    readme = (ROOT_DIR / "README.md").read_text(encoding="utf-8")
    assert "https://app.weio.ai/workspace/keys" in readme
    assert BARE_KEYS_URL_RE.search(readme) is None


def test_r6_cli_source_uses_workspace_keys_and_excludes_bare_404_keys_url():
    cli_src = (ROOT_DIR / "src" / "weio_cli" / "cli.py").read_text(encoding="utf-8")
    assert "https://app.weio.ai/workspace/keys" in cli_src
    assert BARE_KEYS_URL_RE.search(cli_src) is None


def test_r6_readme_matches_closed_beta_claims_only_truth():
    readme = (ROOT_DIR / "README.md").read_text(encoding="utf-8")
    assert "closed beta" in readme.lower()
    assert "plan\npurchases are paused" in readme or "plan purchases are paused" in readme
    assert "weio login                 # get a short code" in readme
    assert "weio login --loopback" in readme
    assert "https://app.weio.ai/cli/device" in readme
    assert "https://weio.ai/download" in readme
    assert "Weio is run by AI agents, with a human founder." in readme
    for forbidden in (
        "is live now",
        "Active open consumer tiers",
        "UNSIGNED",
        "standalone prebuilt binaries and platform packages are distributed",
        "https://app.weio.ai/cli/auth",
        "weio.ai/pricing",
    ):
        assert forbidden not in readme, f"forbidden claim string found: {forbidden}"


def test_r6_readme_limits_platform_claim_to_linux_python_and_discloses_ai():
    readme = (ROOT_DIR / "README.md").read_text(encoding="utf-8")
    assert "The verified install and API-key workflow covers Linux with Python." in readme
    assert "The wheel\nand source distribution are Python packages." in readme
    assert "See [Downloads](https://weio.ai/download)" in readme
    assert "Weio is run by AI agents, with a human founder." in readme


def test_r6_login_no_browser_prompt_displays_workspace_keys(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("WEIO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("WEIO_BASE", "https://example.test/v1")
    monkeypatch.setenv("WEIO_NO_UPDATE_CHECK", "1")
    monkeypatch.delenv("WEIO_API_KEY", raising=False)

    class FakeResp:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/models"):
            return FakeResp({"data": []}, 200)
        if url.endswith("/usage"):
            return FakeResp({"email": "beta" + chr(64) + "example.test", "tier": "lite", "org_name": None}, 200)
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr("httpx.get", fake_get)
    synth_key = "weio_" + "sk_r6_beta_fixture"
    monkeypatch.setattr("builtins.input", lambda prompt="": synth_key)

    rc = cli.main(["login", "--no-browser"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "https://app.weio.ai/workspace/keys" in out
    assert BARE_KEYS_URL_RE.search(out) is None


def test_r6_login_parser_includes_device_flag_and_mentions_workspace_keys():
    parser = cli.build_parser()
    subparsers_actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    login_parser = subparsers_actions[0].choices["login"]
    help_text = login_parser.format_help()
    assert "--device" in help_text
    assert "--loopback" in help_text
    assert "https://app.weio.ai/cli/device" in help_text
    assert "--no-browser" in help_text
    assert "--api-key" in help_text
    assert "https://app.weio.ai/workspace/keys" in help_text
    assert (ROOT_DIR / "src" / "weio_cli" / "device_login.py").exists()
