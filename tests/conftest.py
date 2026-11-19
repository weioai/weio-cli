"""Keep this isolated launch check offline and away from native user config."""
import socket
import pytest


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    monkeypatch.setenv('WEIO_CONFIG_DIR', str(tmp_path / 'weio-config'))
    monkeypatch.setenv('WEIO_NO_UPDATE_CHECK', '1')
    monkeypatch.delenv('WEIO_API_KEY', raising=False)

    def deny(*args, **kwargs):
        raise AssertionError('Network is forbidden in this offline launch check')

    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)

