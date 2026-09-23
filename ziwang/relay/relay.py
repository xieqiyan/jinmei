#!/usr/bin/env python3
"""UDP endpoint Relay for one NS and its CKL/XTL/ZZW devices.

The Relay terminates each UDP hop. It never rewrites an IPv4 packet or
constructs an Ethernet frame. The NS address is carried in the RLY1
application envelope sent to a device.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import selectors
import socket
import struct
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

try:
    from .relay_envelope import decode as decode_envelope, encode as encode_envelope
except ImportError:
    from relay_envelope import decode as decode_envelope, encode as encode_envelope

RADIO_PORT = 10001
QUERY_RESPONSE_PORT = 10009
EVENT_PORT = 8882
QUERY_MSG_TYPE = 0x0A
QUERY_SUPER_OPT = 0x01
QUERY_LEN = 157
RADIO_TYPES = {1: "ckl", 2: "xtl", 3: "zzw"}
QUERY_TYPES = {1: "ckl", 2: "xtl", 4: "zzw"}
DEVICE_QUERY_CODES = {"ckl": 1, "xtl": 2, "zzw": 4}
DEVICE_SHORT_LENGTHS = {"ckl": 9, "xtl": 12, "zzw": 12}
DEVICE_SHORT_MARKERS = {"ckl": b"\x01\x01", "xtl": b"\x01\x02", "zzw": b"\x01\x03"}


@dataclass(frozen=True)
class RelayConfig:
    listen_ip: str
    ns_ip: str
    ns_cidr: ipaddress.IPv4Network
    allowed_ns_ips: frozenset
    devices: Dict[str, str]
    device_listen_ips: Dict[str, str]
    request_port: int = RADIO_PORT
    response_port: int = QUERY_RESPONSE_PORT
    event_port: int = EVENT_PORT
    device_interfaces: Dict[str, str] = None
    ns_interface: str = ""
    log_file: str = ""

    @classmethod
    def load(cls, path: str) -> "RelayConfig":
        with open(path, encoding="utf-8") as file:
            raw = json.load(file)
        relay = raw.get("relay", raw)
        required = set(DEVICE_QUERY_CODES)
        devices = {str(k): str(v) for k, v in relay.get("devices", {}).items()}
        if set(devices) != required:
            raise ValueError(f"devices must contain exactly {sorted(required)}")
        listen_ip = str(relay.get("listenIp", "10.88.0.101"))
        ns_cidr = ipaddress.IPv4Network(relay.get("nsCidr", "10.88.0.0/24"))
        ns_ip = str(relay.get("nsIp", str(next(ns_cidr.hosts()))))
        ipaddress.IPv4Address(listen_ip)
        if ipaddress.IPv4Address(ns_ip) not in ns_cidr:
            raise ValueError(f"nsIp {ns_ip} is outside nsCidr {ns_cidr}")
        if listen_ip == ns_ip:
            raise ValueError("listenIp and nsIp must be different addresses")
        allowed_ns_ips = frozenset(str(value) for value in relay.get("allowedNsIps", [ns_ip]))
        if not allowed_ns_ips or any(ipaddress.IPv4Address(value) not in ns_cidr for value in allowed_ns_ips):
            raise ValueError("allowedNsIps must contain only addresses inside nsCidr")
        device_listen_ips = {str(k): str(v) for k, v in relay.get("deviceListenIps", {}).items()}
        if set(device_listen_ips) != required:
            raise ValueError("deviceListenIps must contain ckl, xtl, zzw")
        all_addresses = [listen_ip, ns_ip, *devices.values(), *device_listen_ips.values()]
        if len(all_addresses) != len(set(all_addresses)):
            raise ValueError("relay, NS, device, and device-listen addresses must be unique")
        for device in required:
            device_ip = ipaddress.IPv4Address(devices[device])
            relay_ip = ipaddress.IPv4Address(device_listen_ips[device])
            if relay_ip not in ipaddress.IPv4Network(f"{device_ip}/24", strict=False):
                raise ValueError(f"deviceListenIps[{device}] must share a subnet with its device")
        ports = [int(relay.get(name, default)) for name, default in (
            ("requestPort", RADIO_PORT), ("responsePort", QUERY_RESPONSE_PORT),
            ("eventPort", EVENT_PORT))]
        if any(port < 1 or port > 65535 for port in ports):
            raise ValueError("Relay ports must be in the range 1..65535")
        return cls(
            listen_ip, ns_ip, ns_cidr, allowed_ns_ips, devices, device_listen_ips,
            ports[0], ports[1], ports[2],
            {str(k): str(v) for k, v in relay.get("deviceInterfaces", {}).items()},
            str(relay.get("nsInterface", "")), str(relay.get("logFile", "")),
        )


def classify_payload(data: bytes) -> Optional[str]:
    if len(data) >= 3 and data[:2] == bytes((QUERY_MSG_TYPE, QUERY_SUPER_OPT)):
        return QUERY_TYPES.get(data[2])
    if len(data) >= 2 and data[:1] == b"\x01":
        return RADIO_TYPES.get(data[1])
    return None


def validate_payload(device: str, data: bytes) -> bool:
    if data[:2] == bytes((QUERY_MSG_TYPE, QUERY_SUPER_OPT)):
        return len(data) >= QUERY_LEN and data[2] == DEVICE_QUERY_CODES[device]
    length = DEVICE_SHORT_LENGTHS[device]
    marker = DEVICE_SHORT_MARKERS[device]
    return bool(data) and len(data) % length == 0 and all(
        data[offset:offset + 2] == marker for offset in range(0, len(data), length)
    )


def business_name(data: bytes) -> str:
    if data[:2] == bytes((QUERY_MSG_TYPE, QUERY_SUPER_OPT)):
        return "query"
    if data[:1] == b"\x01":
        return "inject"
    return "unknown"


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _transport_checksum(packet: bytearray, ihl: int, total_len: int,
                        protocol: int, src: bytes, dst: bytes) -> None:
    if protocol not in (socket.IPPROTO_UDP, socket.IPPROTO_TCP) or total_len < ihl + 8:
        return
    offset = ihl + (16 if protocol == socket.IPPROTO_TCP else 6)
    if offset + 2 > total_len:
        return
    packet[offset:offset + 2] = b"\x00\x00"
    segment = bytes(packet[ihl:total_len])
    pseudo = src + dst + struct.pack("!BBH", 0, protocol, len(segment))
    packet[offset:offset + 2] = struct.pack("!H", _checksum(pseudo + segment) or 0xFFFF)


def rewrite_ipv4_packet(packet: bytes, src_ip: Optional[str] = None,
                        dst_ip: Optional[str] = None) -> Optional[bytes]:
    """Compatibility helper for old callers; the UDP Relay does not use it."""
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return None
    ihl = (packet[0] & 0x0F) * 4
    total_len = struct.unpack_from("!H", packet, 2)[0]
    if ihl < 20 or total_len < ihl or total_len > len(packet):
        return None
    result = bytearray(packet[:total_len])
    src = ipaddress.IPv4Address(src_ip).packed if src_ip else bytes(result[12:16])
    dst = ipaddress.IPv4Address(dst_ip).packed if dst_ip else bytes(result[16:20])
    result[12:16], result[16:20] = src, dst
    result[10:12] = b"\x00\x00"
    result[10:12] = struct.pack("!H", _checksum(bytes(result[:ihl])))
    _transport_checksum(result, ihl, total_len, result[9], src, dst)
    return bytes(result)


def build_udp_ipv4_packet(src_ip: str, dst_ip: str, src_port: int,
                          dst_port: int, payload: bytes) -> bytes:
    src = ipaddress.IPv4Address(src_ip).packed
    dst = ipaddress.IPv4Address(dst_ip).packed
    udp_len = 8 + len(payload)
    udp = struct.pack("!HHHH", src_port, dst_port, udp_len, 0)
    checksum = _checksum(src + dst + struct.pack("!BBH", 0, 17, udp_len) + udp + payload) or 0xFFFF
    udp = struct.pack("!HHHH", src_port, dst_port, udp_len, checksum)
    total_len = 20 + udp_len
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, total_len, 0, 0x4000, 64, 17, 0, src, dst)
    return header[:10] + struct.pack("!H", _checksum(header)) + header[12:] + udp + payload


class UdpRelay:
    def __init__(self, config: RelayConfig):
        self.config = config
        self.selector = selectors.DefaultSelector()
        self.log = logging.getLogger("relay")
        self.ns_request = self._bind(config.listen_ip, config.request_port)
        self.ns_responses = self._bind(config.listen_ip, config.response_port)
        self.ns_events = self._bind(config.listen_ip, config.event_port)
        self.device_requests: Dict[str, socket.socket] = {}
        self.device_responses: Dict[socket.socket, Tuple[str, int]] = {}
        self.device_events: Dict[socket.socket, Tuple[str, int]] = {}
        self.selector.register(self.ns_request, selectors.EVENT_READ, self._on_ns_request)
        for device, relay_ip in config.device_listen_ips.items():
            request = self._bind(relay_ip, 0)
            self.device_requests[device] = request
            response = self._bind(relay_ip, config.response_port)
            event = self._bind(relay_ip, config.event_port)
            self.device_responses[response] = (device, config.response_port)
            self.device_events[event] = (device, config.event_port)
            self.selector.register(response, selectors.EVENT_READ, self._on_device_packet)
            self.selector.register(event, selectors.EVENT_READ, self._on_device_packet)
        self.selector.register(self.ns_responses, selectors.EVENT_READ, self._on_ns_sink)
        self.selector.register(self.ns_events, selectors.EVENT_READ, self._on_ns_sink)

    @staticmethod
    def _bind(address: str, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((address, port))
        return sock

    def _on_ns_request(self, sock: socket.socket) -> None:
        data, peer = sock.recvfrom(65535)
        ns_ip = peer[0]
        try:
            source = ipaddress.IPv4Address(ns_ip)
        except ipaddress.AddressValueError:
            return
        if source not in self.config.ns_cidr or ns_ip not in self.config.allowed_ns_ips or source == ipaddress.IPv4Address(self.config.listen_ip):
            self.log.warning("DROP direction=ns->device reason=source_outside_ns_cidr src=%s", ns_ip)
            return
        device = classify_payload(data)
        if not device or not validate_payload(device, data):
            self.log.warning("DROP direction=ns->device reason=unknown_or_invalid src=%s bytes=%d", ns_ip, len(data))
            return
        reply_port = self.config.response_port if data[:2] == bytes((QUERY_MSG_TYPE, QUERY_SUPER_OPT)) else self.config.event_port
        envelope = encode_envelope(data, ns_ip, device, reply_port, self.config.request_port)
        self.device_requests[device].sendto(envelope, (self.config.devices[device], self.config.request_port))
        self.log.info("FORWARD direction=ns->device business=%s ns=%s relay=%s device=%s bytes=%d",
                      business_name(data), ns_ip, self.config.device_listen_ips[device],
                      self.config.devices[device], len(data))

    def _on_device_packet(self, sock: socket.socket) -> None:
        mapping = self.device_responses.get(sock) or self.device_events.get(sock)
        if mapping is None:
            return
        device, port = mapping
        data, peer = sock.recvfrom(65535)
        if peer[0] != self.config.devices[device]:
            self.log.warning("DROP direction=device->ns device=%s reason=unexpected_peer src=%s", device, peer[0])
            return
        try:
            envelope = decode_envelope(data)
        except ValueError as exc:
            self.log.warning("DROP direction=device->ns device=%s reason=bad_envelope error=%s", device, exc)
            return
        if envelope is None:
            ns_ip = self.config.ns_ip
            payload = data
            reply_port = port
        else:
            if envelope.device != device:
                self.log.warning("DROP direction=device->ns device=%s reason=wrong_envelope_device", device)
                return
            ns_ip = envelope.ns_ip
            payload = envelope.payload
            reply_port = envelope.reply_port
        if ipaddress.IPv4Address(ns_ip) not in self.config.ns_cidr or ns_ip not in self.config.allowed_ns_ips:
            self.log.warning("DROP direction=device->ns device=%s reason=ns_outside_cidr ns=%s", device, ns_ip)
            return
        self._send_to_ns(payload, ns_ip, reply_port)
        self.log.info("FORWARD direction=device->ns device=%s ns=%s relay=%s bytes=%d",
                      device, ns_ip, self.config.listen_ip, len(payload))

    def _send_to_ns(self, payload: bytes, ns_ip: str, port: int) -> None:
        if port not in (self.config.response_port, self.config.event_port):
            self.log.warning("DROP direction=device->ns reason=invalid_reply_port port=%s", port)
            return
        sock = self.ns_responses if port == self.config.response_port else self.ns_events
        sock.sendto(payload, (ns_ip, port))

    def _on_ns_sink(self, sock: socket.socket) -> None:
        # The NS side sockets are intentionally sinks for stray packets. Responses
        # are consumed by the NS application, not by the Relay event loop.
        sock.recvfrom(65535)

    def run(self) -> None:
        while True:
            for key, _ in self.selector.select(timeout=1.0):
                key.data(key.fileobj)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="relay/relay_config.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = RelayConfig.load(args.config)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler()] + ([logging.FileHandler(config.log_file, encoding="utf-8")] if config.log_file else []),
    )
    logging.info("loaded UDP Relay ns_cidr=%s relay_ns=%s devices=%s", config.ns_cidr, config.listen_ip, config.devices)
    if args.dry_run:
        return
    relay = UdpRelay(config)
    relay.run()


if __name__ == "__main__":
    main()
