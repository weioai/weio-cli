"""A failed reauthentication must not overwrite a working saved key."""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from weio_cli import cli, config


class Response:
    def __init__(self, status):
        self.status_code = status
        self.text = '{}'

    def json(self):
        return {}


@pytest.mark.parametrize('status', [401, 403, 500, 503])
def test_failed_reauth_preserves_saved_credential(monkeypatch, tmp_path, capsys, status):
    monkeypatch.setenv('WEIO_BASE', 'https://example.test/v1')
    config.save(key='weio_sk_working_fixture', base='https://example.test/v1',
                source='browser', account_email='previous@example.test', account_tier='pro')
    before = config._file().read_bytes()
    monkeypatch.setattr(httpx, 'get', lambda *args, **kwargs: Response(status))
    result = cli.main(['login', '--api-key', 'weio_sk_rejected_fixture'])
    captured = capsys.readouterr()
    assert result != 0
    assert config._file().read_bytes() == before
    assert 'Saved credentials' not in captured.out
    assert 'weio_sk_working_fixture' not in captured.out + captured.err
    assert 'weio_sk_rejected_fixture' not in captured.out + captured.err


def test_unreachable_gateway_does_not_create_credential(monkeypatch, capsys):
    monkeypatch.setenv('WEIO_BASE', 'https://example.test/v1')

    def fail(*args, **kwargs):
        raise httpx.ConnectError('synthetic offline failure')

    monkeypatch.setattr(httpx, 'get', fail)
    assert cli.main(['login', '--api-key', 'weio_sk_new_fixture']) != 0
    assert not config._file().exists()
    assert 'Saved credentials' not in capsys.readouterr().out


@pytest.mark.parametrize('usage_status, payload', [(401, {}), (403, {}), (503, {}), (200, []), (200, {'success': False})])
def test_models_success_does_not_override_failed_identity(monkeypatch, capsys, usage_status, payload):
    monkeypatch.setenv('WEIO_BASE', 'https://example.test/v1')
    config.save(key='weio_sk_previous_fixture', source='browser', account_email='old@example.test')
    before = config._file().read_bytes()

    class VariedResponse(Response):
        def __init__(self, status, value):
            super().__init__(status)
            self.value = value

        def json(self):
            return self.value

    def get(url, **kwargs):
        if url.endswith('/models'):
            return VariedResponse(200, {'data': []})
        if url.endswith('/usage'):
            return VariedResponse(usage_status, payload)
        raise AssertionError('unexpected URL')

    monkeypatch.setattr(httpx, 'get', get)
    assert cli.main(['login', '--api-key', 'weio_sk_replacement_fixture']) != 0
    assert config._file().read_bytes() == before
    output = capsys.readouterr()
    assert 'Saved credentials' not in output.out
