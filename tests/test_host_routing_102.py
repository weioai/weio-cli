# Unit tests for weio-cli 1.0.2 version and Path B host split routing.
from __future__ import annotations

from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import weio_cli  # noqa: E402
from weio_cli import browser_login  # noqa: E402
from weio_cli.client import WeioClient  # noqa: E402


def test_version_is_current():
    assert weio_cli.__version__ in ("1.0.2", "1.0.3", "1.0.4")


@pytest.mark.parametrize("api_base,expected", [
    ("https://api.weio.ai/v1", "https://app.weio.ai"),
    ("https://api.weio.ai/v1/", "https://app.weio.ai"),
    ("https://weio.ai/v1", "https://app.weio.ai"),
    ("https://www.weio.ai/v1", "https://app.weio.ai"),
    ("https://app.weio.ai/v1", "https://app.weio.ai"),
    ("http://127.0.0.1:8913/v1", "http://127.0.0.1:8913"),
    ("https://example.test/v1", "https://example.test"),
])
def test_web_base_from_api(api_base, expected):
    assert browser_login._web_base_from_api(api_base) == expected


@pytest.mark.parametrize("api_base,expected_prefix", [
    ("https://api.weio.ai/v1", "https://app.weio.ai/api/public/ledger"),
    ("https://weio.ai/v1", "https://app.weio.ai/api/public/ledger"),
    ("https://www.weio.ai/v1", "https://app.weio.ai/api/public/ledger"),
    ("https://example.test/v1", "https://example.test/api/public/ledger"),
    ("http://127.0.0.1:8913/v1", "http://127.0.0.1:8913/api/public/ledger"),
])
def test_client_public_url(api_base, expected_prefix):
    c = WeioClient("weio_sk_test", api_base)
    assert c._public_url("ledger") == expected_prefix
