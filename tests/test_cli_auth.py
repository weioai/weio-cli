"""Tests for CLI auth v2: `weio logout`, `weio whoami`, and the tier-split
`weio login` (device default, --loopback, --api-key / WEIO_API_KEY dashboard path).

All HTTP is mocked (httpx.get); the browser loopback flow is mocked at the
`weio_cli.browser_login.browser_login` function so no real socket/browser is
touched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weio_cli import cli, config  # noqa: E402


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def _mock_http(monkeypatch, usage_payload=None, usage_status=200, models_status=200):
    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/models"):
            return FakeResponse({"data": []}, status_code=models_status)
        if url.endswith("/usage"):
            return FakeResponse(usage_payload or {}, status_code=usage_status)
        raise AssertionError(f"unexpected GET {url}")
    monkeypatch.setattr("httpx.get", fake_get)


def _common_env(monkeypatch, tmp_path):
    monkeypatch.setenv("WEIO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("WEIO_BASE", "https://example.test/v1")
    monkeypatch.setenv("WEIO_NO_UPDATE_CHECK", "1")
    monkeypatch.delenv("WEIO_API_KEY", raising=False)


# --------------------------------------------------------------------------- #
# logout
# --------------------------------------------------------------------------- #

def test_logout_idempotent_when_not_logged_in(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    rc = cli.main(["logout"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not logged in" in out.lower()


def test_logout_clears_only_auth_fields(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    config.save(key="weio_sk_old", base="https://custom.example/v1", source="browser",
               account_email="old@example.test", account_tier="starter")

    rc = cli.main(["logout"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "old@example.test" in out
    data = json.loads((tmp_path / "config.json").read_text())
    assert "api_key" not in data
    assert "key_source" not in data
    assert "account_email" not in data
    assert "account_tier" not in data
    # base is not a credential — logout must not touch it.
    assert data["base"] == "https://custom.example/v1"


def test_logout_a_second_time_is_idempotent(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    config.save(key="weio_sk_old", source="api_key", account_email="a@example.test")
    cli.main(["logout"])
    capsys.readouterr()
    rc = cli.main(["logout"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not logged in" in out.lower()


def test_logout_notes_env_var_still_active(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    config.save(key="weio_sk_old", source="browser", account_email="a@example.test")
    monkeypatch.setenv("WEIO_API_KEY", "weio_sk_from_env")

    rc = cli.main(["logout"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "WEIO_API_KEY" in out


# --------------------------------------------------------------------------- #
# whoami
# --------------------------------------------------------------------------- #

def test_whoami_reports_browser_session_key(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    config.save(key="weio_sk_browser", source="browser")
    _mock_http(monkeypatch, {"email": "me@example.test", "tier": "pro", "org_name": None})

    rc = cli.main(["whoami"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "me@example.test" in out
    assert "pro" in out
    assert "session key (browser sign-in)" in out
    assert "https://example.test/v1" in out


def test_whoami_reports_dashboard_api_key_and_org(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    config.save(key="weio_sk_dashboard", source="api_key")
    _mock_http(monkeypatch, {"email": "lead@acme.test", "tier": "team", "org_name": "Acme Inc"})

    rc = cli.main(["whoami"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "dashboard API key" in out
    assert "Acme Inc" in out
    assert "team" in out


def test_whoami_reports_env_var_key(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    monkeypatch.setenv("WEIO_API_KEY", "weio_sk_env")
    _mock_http(monkeypatch, {"email": "ci@example.test", "tier": "team", "org_name": None})

    rc = cli.main(["whoami"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "dashboard API key (WEIO_API_KEY)" in out
    assert "ci@example.test" in out


def test_whoami_reports_flag_key(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    _mock_http(monkeypatch, {"email": "adhoc@example.test", "tier": "starter", "org_name": None})

    rc = cli.main(["whoami", "--key", "weio_sk_flag"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "dashboard API key (--key flag)" in out


def test_whoami_invalid_key_reports_error(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    config.save(key="weio_sk_revoked", source="browser")
    _mock_http(monkeypatch, usage_status=401)

    rc = cli.main(["whoami"])
    err = capsys.readouterr().err

    assert rc == 1
    assert "401" in err or "authentication failed" in err.lower()


# --------------------------------------------------------------------------- #
# login: --api-key path, re-auth diff, Teams note
# --------------------------------------------------------------------------- #

def test_login_api_key_flag_persists_source_and_identity(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    _mock_http(monkeypatch, {"email": "dev@example.test", "tier": "pro", "org_name": None})

    rc = cli.main(["login", "--api-key", "weio_sk_dashboard_issued"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Saved credentials" in out
    data = json.loads((tmp_path / "config.json").read_text())
    assert data["api_key"] == "weio_sk_dashboard_issued"
    assert data["key_source"] == "api_key"
    assert data["account_email"] == "dev@example.test"
    assert data["account_tier"] == "pro"


def test_login_no_browser_paste_source_is_api_key(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    _mock_http(monkeypatch, {"email": "dev@example.test", "tier": "starter", "org_name": None})
    monkeypatch.setattr("builtins.input", lambda prompt="": "weio_sk_pasted")

    rc = cli.main(["login", "--no-browser"])
    capsys.readouterr()

    assert rc == 0
    data = json.loads((tmp_path / "config.json").read_text())
    assert data["key_source"] == "api_key"


def test_login_browser_flow_tags_source_browser(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    _mock_http(monkeypatch, {"email": "dev@example.test", "tier": "starter", "org_name": None})
    monkeypatch.setattr("weio_cli.browser_login.browser_login", lambda base, timeout=180.0: "weio_sk_browser_minted")

    rc = cli.main(["login","--loopback"])
    capsys.readouterr()

    assert rc == 0
    data = json.loads((tmp_path / "config.json").read_text())
    assert data["key_source"] == "browser"


def test_reauth_reports_identity_change(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    config.save(key="weio_sk_old", source="browser", account_email="old@example.test",
               account_tier="starter")
    _mock_http(monkeypatch, {"email": "new@example.test", "tier": "pro", "org_name": None})

    rc = cli.main(["login", "--api-key", "weio_sk_new"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Was signed in as old@example.test, now new@example.test." in out


def test_first_login_has_no_identity_diff_message(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    _mock_http(monkeypatch, {"email": "first@example.test", "tier": "starter", "org_name": None})

    rc = cli.main(["login", "--api-key", "weio_sk_first"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Was signed in as" not in out


def test_reauth_same_identity_has_no_diff_message(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    config.save(key="weio_sk_old", source="browser", account_email="same@example.test",
               account_tier="starter")
    _mock_http(monkeypatch, {"email": "same@example.test", "tier": "starter", "org_name": None})

    rc = cli.main(["login", "--api-key", "weio_sk_new"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Was signed in as" not in out


def test_login_team_tier_via_browser_notes_dashboard_key(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    _mock_http(monkeypatch, {"email": "lead@acme.test", "tier": "team", "org_name": "Acme Inc"})
    monkeypatch.setattr("weio_cli.browser_login.browser_login", lambda base, timeout=180.0: "weio_sk_browser_minted")

    rc = cli.main(["login","--loopback"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "dashboard API key" in out
    assert "recommended" in out.lower()


def test_login_pro_tier_via_browser_has_no_extra_note(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    _mock_http(monkeypatch, {"email": "solo@example.test", "tier": "pro", "org_name": None})
    monkeypatch.setattr("weio_cli.browser_login.browser_login", lambda base, timeout=180.0: "weio_sk_browser_minted")

    rc = cli.main(["login","--loopback"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "recommended" not in out.lower()


def test_login_via_api_key_flag_skips_teams_note_even_on_team_tier(monkeypatch, tmp_path, capsys):
    """Already using a dashboard key — no need to nag them to use one."""
    _common_env(monkeypatch, tmp_path)
    _mock_http(monkeypatch, {"email": "lead@acme.test", "tier": "team", "org_name": "Acme Inc"})

    rc = cli.main(["login", "--api-key", "weio_sk_dashboard"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "recommended" not in out.lower()


def test_login_device_flow_tags_source_device_and_whoami_reports_cli_agent_key(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    _mock_http(monkeypatch, {"email": "dev@example.test", "tier": "lite", "org_name": None})
    monkeypatch.setattr("weio_cli.device_login.device_login", lambda base, timeout=600.0, open_browser=True: "weio_sk_device_minted")

    rc = cli.main(["login"])
    capsys.readouterr()

    assert rc == 0
    assert (tmp_path / "config.json").stat().st_mode & 0o777 == 0o600
    data = json.loads((tmp_path / "config.json").read_text())
    assert data["api_key"] == "weio_sk_device_minted"
    assert data["key_source"] == "device"

    rc_who = cli.main(["whoami"])
    out_who = capsys.readouterr().out
    assert rc_who == 0
    assert "CLI and coding agent key (device-code sign-in)" in out_who


def test_login_device_flow_failure_preserves_existing_credential(monkeypatch, tmp_path, capsys):
    _common_env(monkeypatch, tmp_path)
    config.save(key="weio_sk_existing", source="api_key", account_email="keep@example.test", account_tier="lite")
    before = (tmp_path / "config.json").read_bytes()
    monkeypatch.setattr("weio_cli.device_login.device_login", lambda base, timeout=600.0, open_browser=True: None)

    rc = cli.main(["login", "--device"])
    capsys.readouterr()
    assert rc == 1
    assert (tmp_path / "config.json").read_bytes() == before
