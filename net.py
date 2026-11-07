"""LAN IPv4 detection for the "Open to phones" step.

Only private IPv4 addresses (10/8, 172.16/12, 192.168/16) of interfaces that are up are considered;
the interface that carries the default route is preferred. The server never binds every interface:
it adds one listening site on the single address returned here. psutil (a Hermes core dependency) is
imported lazily; without it we fall back to the addresses the host name resolves to.
No outbound connection is ever made to find the address.
"""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path

_PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)

# Interfaces that are almost never what phones in the room can reach.
_VIRTUAL_PREFIXES = ("docker", "br-", "veth", "virbr", "vmnet", "vboxnet", "cni", "flannel", "kube", "podman", "lxc", "lxd", "zt")


def is_private_ipv4(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.version == 4 and any(addr in net for net in _PRIVATE_NETS)


def is_loopback(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    mapped = getattr(addr, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def default_route_interface(route_table: str | None = None) -> str | None:
    """Name of the interface holding the IPv4 default route (Linux /proc/net/route), else None."""
    if route_table is None:
        try:
            route_table = Path("/proc/net/route").read_text()
        except OSError:
            return None
    for line in route_table.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "00000000":
            return parts[0]
    return None


def _psutil_candidates() -> list[tuple[str, str]] | None:
    try:
        import psutil  # lazy: Hermes core dependency, optional here
    except Exception:
        return None
    out: list[tuple[str, str]] = []
    try:
        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            st = stats.get(name)
            if st is not None and not st.isup:
                continue
            for a in addrs:
                if a.family == socket.AF_INET:
                    out.append((name, a.address))
    except Exception:
        return None
    return out


def lan_candidates(interfaces: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    """(interface, ipv4) pairs that are private, in preference order."""
    if interfaces is None:
        interfaces = _psutil_candidates()
        if interfaces is None:
            interfaces = []
            try:
                for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                    interfaces.append(("?", str(info[4][0])))
            except OSError:
                pass
    seen: set[str] = set()
    out = []
    for name, ip in interfaces:
        if ip in seen or not is_private_ipv4(ip):
            continue
        seen.add(ip)
        out.append((name, ip))
    return out


def detect_lan_ipv4(interfaces: list[tuple[str, str]] | None = None, default_iface: str | None = None) -> str | None:
    """Best private IPv4 for phones in the room, or None.

    Order: interface of the default route, then physical-looking interfaces, then the rest.
    """
    cands = lan_candidates(interfaces)
    if not cands:
        return None
    if default_iface is None:
        default_iface = default_route_interface()

    def rank(item: tuple[int, tuple[str, str]]) -> tuple[int, int, int]:
        pos, (name, ip) = item
        virtual = 1 if name.startswith(_VIRTUAL_PREFIXES) else 0
        return (0 if default_iface and name == default_iface else 1, virtual, pos)

    return sorted(enumerate(cands), key=rank)[0][1][1]
