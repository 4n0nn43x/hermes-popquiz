"""LAN detection (no real network access needed)."""

from __future__ import annotations

from popquiz_plugin import net

ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
wlp2s0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0
wlp2s0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0
"""


def test_private_ranges():
    assert net.is_private_ipv4("10.1.2.3") and net.is_private_ipv4("172.20.0.1") and net.is_private_ipv4("192.168.0.9")
    assert not net.is_private_ipv4("172.32.0.1") and not net.is_private_ipv4("8.8.8.8")
    assert not net.is_private_ipv4("127.0.0.1") and not net.is_private_ipv4("fe80::1") and not net.is_private_ipv4("x")


def test_loopback():
    assert net.is_loopback("127.0.0.1") and net.is_loopback("127.9.9.9") and net.is_loopback("::1")
    assert net.is_loopback("::ffff:127.0.0.1")
    assert not net.is_loopback("192.168.1.2") and not net.is_loopback("") and not net.is_loopback(None)
    assert not net.is_loopback("garbage")


def test_default_route_interface_parse():
    assert net.default_route_interface(ROUTE) == "wlp2s0"
    assert net.default_route_interface("Iface\tDestination\n") is None


def test_prefers_default_route_interface():
    ifaces = [("docker0", "172.17.0.1"), ("eth9", "10.0.0.5"), ("wlp2s0", "192.168.1.42"), ("lo", "127.0.0.1")]
    assert net.detect_lan_ipv4(ifaces, default_iface="wlp2s0") == "192.168.1.42"


def test_skips_virtual_and_public():
    ifaces = [("docker0", "172.17.0.1"), ("ppp0", "84.1.2.3"), ("enp3s0", "10.0.0.5")]
    assert net.detect_lan_ipv4(ifaces, default_iface="ppp0") == "10.0.0.5"


def test_none_when_no_private_address():
    assert net.detect_lan_ipv4([("lo", "127.0.0.1"), ("ppp0", "84.1.2.3")], default_iface="ppp0") is None
    assert net.lan_candidates([("a", "10.0.0.1"), ("b", "10.0.0.1")]) == [("a", "10.0.0.1")]


def test_real_detection_never_raises():
    ip = net.detect_lan_ipv4()
    assert ip is None or net.is_private_ipv4(ip)
