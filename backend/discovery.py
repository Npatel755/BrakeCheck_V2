"""Advertise the local FastAPI service through Bonjour/mDNS."""
import ipaddress
import os
import socket
import logging

log = logging.getLogger(__name__)


def _local_ipv4() -> str:
    # A UDP connect selects the active interface without sending application data.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('192.0.2.1', 9))
            address = sock.getsockname()[0]
            if ipaddress.ip_address(address).is_private:
                return address
    except OSError:
        pass

    candidates = []
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = item[4][0]
            parsed = ipaddress.ip_address(address)
            if parsed.is_private and not parsed.is_loopback and not parsed.is_link_local:
                candidates.append(address)
    except OSError:
        pass
    if not candidates:
        raise RuntimeError('No private IPv4 network address was found.')
    return candidates[0]


class BonjourAdvertiser:
    def __init__(self):
        self._zeroconf = None
        self._info = None

    def start(self):
        from zeroconf import IPVersion, ServiceInfo, Zeroconf

        address = _local_ipv4()
        host = ''.join(ch if ch.isalnum() or ch == '-' else '-' for ch in socket.gethostname()).strip('-').lower()
        host = host or 'computer'
        service_type = '_brakecheck._tcp.local.'
        service_name = f'BrakeCheck on {host}.{service_type}'
        port = int(os.getenv('BRAKECHECK_PORT', '8000'))
        self._info = ServiceInfo(
            service_type,
            service_name,
            addresses=[socket.inet_aton(address)],
            port=port,
            properties={b'version': b'1'},
            server=f'{host}.local.',
        )
        self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        self._zeroconf.register_service(self._info, allow_name_change=True)
        log.info('Bonjour discovery advertised on %s:%s', address, port)

    def close(self):
        if self._zeroconf is not None:
            try:
                if self._info is not None:
                    self._zeroconf.unregister_service(self._info)
            except Exception as exc:
                log.debug('Could not unregister Bonjour service cleanly: %s', exc)
            finally:
                try:
                    self._zeroconf.close()
                except Exception as exc:
                    log.debug('Could not close Bonjour service cleanly: %s', exc)
                self._zeroconf = None
                self._info = None
