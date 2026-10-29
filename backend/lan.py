"""Private IPv4 ranges supported by the iPhone's local HTTP connection policy."""
import ipaddress

_PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(cidr) for cidr in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
))


def is_private_ipv4(raw: str) -> bool:
    try:
        address = ipaddress.IPv4Address(raw)
    except ipaddress.AddressValueError:
        return False
    return any(address in network for network in _PRIVATE_NETWORKS)
