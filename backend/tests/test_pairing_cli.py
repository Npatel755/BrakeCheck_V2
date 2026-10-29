"""Local pairing validation must agree with the phone before making a QR."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pair
import discovery
from lan import is_private_ipv4


@pytest.mark.parametrize('host', ['10.0.0.1', '172.16.0.1', '172.31.255.254', '192.168.1.2'])
def test_private_lan_addresses(host):
    assert is_private_ipv4(host)
    assert pair.normalize_backend_url(f'http://{host}:8000/') == f'http://{host}:8000'


@pytest.mark.parametrize('host', ['0.0.0.0', '127.0.0.1', '169.254.1.2', '192.0.2.1', '198.51.100.1',
    '203.0.113.1', '8.8.8.8', '172.15.1.1', '172.32.1.1', 'localhost', 'pc.localhost', 'computer.local',
    '10.1.2.3.example.com', '010.1.2.3', '10.1.2', '10..1.2.3', '::1', 'fc00::1'])
def test_unusable_http_addresses_are_rejected(host):
    assert not is_private_ipv4(host)
    with pytest.raises(ValueError):
        pair.normalize_backend_url(f'http://[{host}]:8000' if ':' in host else f'http://{host}:8000')


@pytest.mark.parametrize('address', ['https://example.com:0', 'https://example.com:65536',
    'https://example.com:bad', 'https://user:secret@example.com', 'https://example.com/api',
    'https://example.com?key=value', 'https://example.com#fragment'])
def test_invalid_base_urls_are_rejected(address):
    with pytest.raises(ValueError):
        pair.normalize_backend_url(address)


def test_https_cloud_address_is_preserved():
    assert pair.normalize_backend_url(' https://backend.example.com/ ') == 'https://backend.example.com'


@pytest.mark.parametrize('result', [[], None, {}, {'code': 7}, {'code': ''}, {'code': 'x' * 129}])
def test_malformed_pairing_response_does_not_create_a_qr(monkeypatch, result):
    monkeypatch.setenv('BRAKECHECK_PUBLIC_URL', '')
    monkeypatch.setenv('BRAKECHECK_APP_KEY', 'offline-test-key-000000000000000000')
    monkeypatch.setenv('BRAKECHECK_PORT', '8000')
    monkeypatch.setattr('builtins.input', lambda *args: '192.168.1.2')
    class Response:
        def raise_for_status(self): pass
        def json(self): return result
    monkeypatch.setattr(pair.requests, 'post', lambda *args, **kwargs: Response())
    def forbidden(*args):
        raise AssertionError('Invalid code must not be shown in a QR')
    monkeypatch.setattr(pair.qrcode, 'make', forbidden)
    with pytest.raises(SystemExit, match='unexpected pairing response'):
        pair.main([])


@pytest.mark.parametrize('port', ['bad', '0', '-1', '65536'])
def test_invalid_local_port_has_clear_error(monkeypatch, port):
    monkeypatch.setenv('BRAKECHECK_PUBLIC_URL', '')
    monkeypatch.setenv('BRAKECHECK_PORT', port)
    with pytest.raises(SystemExit, match='BRAKECHECK_PORT'):
        pair.main([])


def test_discovery_skips_unusable_interface_and_uses_private_fallback(monkeypatch):
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def connect(self, target): pass
        def getsockname(self): return ('0.0.0.0', 1000)
    monkeypatch.setattr(discovery.socket, 'socket', lambda *args: Socket())
    monkeypatch.setattr(discovery.socket, 'getaddrinfo', lambda *args: [
        (None, None, None, None, (host, 0)) for host in ['127.0.0.1', '169.254.1.2', '192.0.2.1', '192.168.1.2']
    ])
    assert discovery._local_ipv4() == '192.168.1.2'
