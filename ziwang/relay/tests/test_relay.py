import ipaddress
import json
import pathlib
import struct
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from relay.relay import (RelayConfig, build_udp_ipv4_packet, classify_payload,
                         rewrite_ipv4_packet, validate_payload)


def test_classify_queries_and_injections():
    assert classify_payload(bytes([0x0A, 0x01, 0x01]) + bytes(154)) == "ckl"
    assert classify_payload(bytes([0x0A, 0x01, 0x02]) + bytes(154)) == "xtl"
    assert classify_payload(bytes([0x0A, 0x01, 0x04]) + bytes(154)) == "zzw"
    assert classify_payload(bytes([0x01, 0x01]) + bytes(7)) == "ckl"
    assert classify_payload(bytes([0x01, 0x02]) + bytes(10)) == "xtl"
    assert classify_payload(bytes([0x01, 0x03]) + bytes(10)) == "zzw"


def test_validate_payload():
    assert validate_payload("ckl", bytes([1, 1]) + bytes(7))
    assert validate_payload("xtl", bytes([1, 2]) + bytes(10) + bytes([1, 2]) + bytes(10))
    assert not validate_payload("ckl", b"\x01\x01")
    assert not validate_payload("xtl", bytes([1, 3]) + bytes(10))


def test_raw_packet_preserves_addresses_and_udp_checksum():
    packet = build_udp_ipv4_packet("10.0.0.7", "10.88.0.1", 12345, 10001, b"abc")
    assert packet[0] == 0x45
    assert str(ipaddress.IPv4Address(packet[12:16])) == "10.0.0.7"
    assert str(ipaddress.IPv4Address(packet[16:20])) == "10.88.0.1"
    assert struct.unpack_from("!HH", packet, 20) == (12345, 10001)
    assert struct.unpack_from("!H", packet, 24)[0] == 11


def test_rewrite_packet_changes_only_destination_and_keeps_udp_payload():
    packet = build_udp_ipv4_packet("10.88.0.1", "10.88.0.101", 41000, 10001, b"payload")
    rewritten = rewrite_ipv4_packet(packet, dst_ip="10.89.1.11")
    assert rewritten is not None
    assert str(ipaddress.IPv4Address(rewritten[12:16])) == "10.88.0.1"
    assert str(ipaddress.IPv4Address(rewritten[16:20])) == "10.89.1.11"
    assert rewritten[20:] == packet[20:]
    assert struct.unpack_from("!H", rewritten, 24)[0] != struct.unpack_from("!H", packet, 24)[0]


def test_rewrite_packet_supports_tcp_checksum():
    src = ipaddress.IPv4Address("10.88.0.1").packed
    dst = ipaddress.IPv4Address("10.88.0.101").packed
    tcp = struct.pack("!HHLLBBHHH", 40000, 10001, 1, 0, 0x50, 0x18, 4096, 0, 0)
    total = 20 + len(tcp)
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, total, 0, 0x4000, 64, 6, 0, src, dst)
    packet = header[:10] + struct.pack("!H", 0) + header[12:] + tcp
    rewritten = rewrite_ipv4_packet(packet, dst_ip="10.89.1.11")
    assert rewritten is not None
    assert rewritten[9] == 6
    assert rewritten[20:22] == tcp[:4]


def test_default_config():
    config = RelayConfig.load(ROOT / "relay" / "relay_config.json")
    assert config.listen_ip == "10.88.0.101"
    assert config.ns_cidr == ipaddress.IPv4Network("10.88.0.0/24")
    assert config.devices["ckl"] == "10.89.1.11"
    assert config.device_listen_ips["ckl"] == "10.89.1.101"


def test_config_rejects_reused_addresses(tmp_path):
    path = tmp_path / "relay.json"
    raw = json.loads((ROOT / "relay" / "relay_config.json").read_text(encoding="utf-8"))
    raw["relay"]["deviceListenIps"]["zzw"] = "10.89.1.101"
    path.write_text(json.dumps(raw), encoding="utf-8")
    try:
        RelayConfig.load(path)
    except ValueError as error:
        assert "unique" in str(error)
    else:
        raise AssertionError("duplicate relay addresses were accepted")
