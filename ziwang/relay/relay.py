#!/usr/bin/env python3
"""Userspace IPv4 relay for one NS and its CKL/XTL/ZZW devices."""
from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import logging
import os
import selectors
import socket
import struct
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

SIOCGIFHWADDR = 0x8927
ETH_P_IP = 0x0800

RADIO_PORT = 10001
QUERY_RESPONSE_PORT = 10009
EVENT_PORT = 8882
QUERY_MSG_TYPE = 0x0A
QUERY_SUPER_OPT = 0x01
QUERY_LEN = 157
RADIO_TYPES = {1: "ckl", 2: "xtl", 3: "zzw"}
QUERY_TYPES = {1: "ckl", 2: "xtl", 4: "zzw"}


@dataclass(frozen=True)
class RelayConfig:
    listen_ip: str
    ns_ip: str
    ns_cidr: ipaddress.IPv4Network
    devices: Dict[str, str]
    device_interfaces: Dict[str, str]
    device_listen_ips: Dict[str, str]
    request_port: int = RADIO_PORT
    response_port: int = QUERY_RESPONSE_PORT
    event_port: int = EVENT_PORT
    interface: str = "relay0"
    ns_interface: str = "relay-ns"
    device_routes: Dict[str, Dict[str, str]] = None
    log_file: str = ""

    @classmethod
    def load(cls, path: str) -> "RelayConfig":
        with open(path, encoding="utf-8") as file:
            raw = json.load(file)
        relay = raw.get("relay", raw)
        required = {"ckl", "xtl", "zzw"}
        devices = relay.get("devices", {})
        if set(devices) != required:
            raise ValueError(f"devices must contain exactly {sorted(required)}")
        listen_ip = str(relay.get("listenIp", "10.88.0.101"))
        ipaddress.IPv4Address(listen_ip)
        ns_cidr = ipaddress.IPv4Network(relay.get("nsCidr", "10.88.0.0/24"))
        ns_ip = str(relay.get("nsIp", str(next(ns_cidr.hosts(), "0.0.0.0"))))
        if ipaddress.IPv4Address(ns_ip) not in ns_cidr:
            raise ValueError(f"nsIp {ns_ip} is outside nsCidr {ns_cidr}")
        if listen_ip == ns_ip:
            raise ValueError("listenIp and nsIp must be different addresses")
        device_interfaces = relay.get("deviceInterfaces", {})
        device_listen_ips = relay.get("deviceListenIps", {})
        if set(device_interfaces) != required or set(device_listen_ips) != required:
            raise ValueError("deviceInterfaces and deviceListenIps must contain ckl, xtl, zzw")
        for value in devices.values():
            ipaddress.IPv4Address(value)
        for value in device_listen_ips.values():
            ipaddress.IPv4Address(value)
        all_addresses = [listen_ip, ns_ip, *devices.values(), *device_listen_ips.values()]
        if len(all_addresses) != len(set(all_addresses)):
            raise ValueError("relay, NS, device, and device-listen addresses must be unique")
        for device in required:
            network = ipaddress.IPv4Network(f"{devices[device]}/24", strict=False)
            if ipaddress.IPv4Address(device_listen_ips[device]) not in network:
                raise ValueError(f"deviceListenIps[{device}] must share a subnet with its device")
        routes = relay.get("deviceRoutes", {})
        device_routes = {device: {str(ns): str(ip) for ns, ip in mapping.items()}
                         for device, mapping in routes.items() if isinstance(mapping, dict)}
        return cls(listen_ip, ns_ip, ns_cidr,
                   {key: str(value) for key, value in devices.items()},
                   {key: str(value) for key, value in device_interfaces.items()},
                   {key: str(value) for key, value in device_listen_ips.items()},
                   int(relay.get("requestPort", RADIO_PORT)),
                   int(relay.get("responsePort", QUERY_RESPONSE_PORT)),
                   int(relay.get("eventPort", EVENT_PORT)),
                   str(relay.get("interface", "relay0")),
                   str(relay.get("nsInterface", "relay-ns")), device_routes,
                   str(relay.get("logFile", "")))


def classify_payload(data: bytes) -> Optional[str]:
    if len(data) >= 3 and data[0] == QUERY_MSG_TYPE and data[1] == QUERY_SUPER_OPT:
        return QUERY_TYPES.get(data[2])
    if len(data) >= 2 and data[0] == 0x01:
        return RADIO_TYPES.get(data[1])
    return None


def validate_payload(device: str, data: bytes) -> bool:
    if data[:2] == bytes((QUERY_MSG_TYPE, QUERY_SUPER_OPT)):
        return len(data) >= QUERY_LEN and data[2] == {"ckl": 1, "xtl": 2, "zzw": 4}[device]
    short_len = {"ckl": 9, "xtl": 12, "zzw": 12}[device]
    marker = bytes((0x01, {"ckl": 1, "xtl": 2, "zzw": 3}[device]))
    return len(data) > 0 and len(data) % short_len == 0 and all(
        data[offset:offset + 2] == marker for offset in range(0, len(data), short_len))


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


def _transport_checksum(packet: bytearray, ihl: int, total_len: int, protocol: int,
                        src: bytes, dst: bytes) -> None:
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


def build_udp_ipv4_packet(src_ip: str, dst_ip: str, src_port: int, dst_port: int,
                          payload: bytes) -> bytes:
    src, dst = ipaddress.IPv4Address(src_ip).packed, ipaddress.IPv4Address(dst_ip).packed
    udp_len = 8 + len(payload)
    udp = struct.pack("!HHHH", src_port, dst_port, udp_len, 0)
    udp = struct.pack("!HHHH", src_port, dst_port, udp_len,
                      _checksum(src + dst + struct.pack("!BBH", 0, 17, udp_len) + udp + payload) or 0xFFFF)
    total_len = 20 + udp_len
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, total_len, 0, 0x4000, 64, 17, 0, src, dst)
    header = header[:10] + struct.pack("!H", _checksum(header)) + header[12:]
    return header + udp + payload


class RawTransmitter:
    def __init__(self, interface: str):
        self.interface = interface
        self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_IP))
        self.sock.bind((interface, 0))
        request = struct.pack("256s", interface.encode()[:15])
        self.source_mac = fcntl.ioctl(self.sock.fileno(), SIOCGIFHWADDR, request)[18:24]

    def send_packet(self, packet: bytes, destination: str) -> None:
        # The relay owns the L3 destination, but the device/NS may not have a
        # usable ARP entry yet. Broadcast on the isolated link lets the
        # receiving OVS bridge deliver by destination IP without ARP.
        frame = b"\xff" * 6 + self.source_mac + struct.pack("!H", ETH_P_IP) + packet
        self.sock.send(frame)


class PacketCapture:
    def __init__(self, interface: str):
        self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0800))
        self.sock.bind((interface, 0))

    def recv(self) -> bytes:
        frame = self.sock.recv(65535)
        return frame[14:] if len(frame) >= 14 and frame[12:14] == b"\x08\x00" else b""


def parse_ipv4(packet: bytes) -> Optional[Tuple[str, str, int, bytes]]:
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return None
    ihl = (packet[0] & 0x0F) * 4
    total_len = struct.unpack_from("!H", packet, 2)[0]
    if ihl < 20 or total_len < ihl or total_len > len(packet):
        return None
    return str(ipaddress.IPv4Address(packet[12:16])), str(ipaddress.IPv4Address(packet[16:20])), packet[9], packet[:total_len]


class TransparentRelay:
    def __init__(self, config: RelayConfig, ns_capture: PacketCapture,
                 device_capture: Dict[str, PacketCapture], raw_device: Dict[str, RawTransmitter],
                 raw_ns: RawTransmitter):
        self.config, self.ns_capture = config, ns_capture
        self.device_capture, self.raw_device, self.raw_ns = device_capture, raw_device, raw_ns
        self.selector = selectors.DefaultSelector()
        self.selector.register(ns_capture.sock, selectors.EVENT_READ, self._ns_packet)
        self.logger = logging.getLogger("relay")

    def _ns_packet(self, sock: socket.socket) -> None:
        packet = self.ns_capture.recv()
        parsed = parse_ipv4(packet)
        if not parsed:
            return
        src, dst, protocol, packet = parsed
        if src != self.config.ns_ip or dst != self.config.listen_ip:
            return
        ihl = (packet[0] & 0x0F) * 4
        payload = packet[ihl + 8:] if protocol == socket.IPPROTO_UDP and len(packet) >= ihl + 8 else packet[ihl:]
        device = classify_payload(payload)
        if not device or not validate_payload(device, payload):
            self.logger.warning("DROP direction=ns->device reason=unknown_or_invalid src=%s dst=%s bytes=%d", src, dst, len(packet))
            return
        target = self.config.devices[device]
        rewritten = rewrite_ipv4_packet(packet, dst_ip=target)
        if rewritten:
            self.raw_device[device].send_packet(rewritten, target)
            self.logger.info("FORWARD direction=ns->device business=%s src=%s dst=%s device=%s bytes=%d", business_name(payload), src, target, device, len(packet))

    def _device_packet(self, sock: socket.socket) -> None:
        capture = next((item for item in self.device_capture.values() if item.sock is sock), None)
        if capture is None:
            return
        parsed = parse_ipv4(capture.recv())
        if not parsed:
            return
        src, dst, _, packet = parsed
        device = next((name for name, ip in self.config.devices.items() if ip == src), None)
        if not device or dst not in {self.config.device_listen_ips[device], self.config.listen_ip, self.config.ns_ip}:
            return
        rewritten = rewrite_ipv4_packet(packet, dst_ip=self.config.ns_ip)
        if rewritten:
            self.raw_ns.send_packet(rewritten, self.config.ns_ip)
            self.logger.info("FORWARD direction=device->ns src=%s dst=%s device=%s bytes=%d", src, self.config.ns_ip, device, len(packet))

    def run(self) -> None:
        for capture in self.device_capture.values():
            self.selector.register(capture.sock, selectors.EVENT_READ, self._device_packet)
        while True:
            for key, _ in self.selector.select(timeout=1.0):
                key.data(key.fileobj)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("RELAY_CONFIG", "relay/relay_config.json"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--log-file", default=os.environ.get("RELAY_LOG_FILE"))
    return parser.parse_args()


def make_udp_sink(bind_ip: str, port: int) -> socket.socket:
    """Prevent the kernel from generating ICMP port-unreachable replies."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_ip, port))
    return sock


def main() -> None:
    args = parse_args()
    config = RelayConfig.load(args.config)
    handlers = [logging.StreamHandler()]
    if args.log_file or config.log_file:
        handlers.append(logging.FileHandler(args.log_file or config.log_file, encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    logging.info("loaded relay config ns=%s relay=%s devices=%s", config.ns_ip, config.listen_ip, config.devices)
    if args.dry_run:
        return
    sink = make_udp_sink(config.listen_ip, config.request_port)
    ns_capture = PacketCapture(config.ns_interface)
    device_capture = {name: PacketCapture(interface) for name, interface in config.device_interfaces.items()}
    raw_device = {name: RawTransmitter(interface) for name, interface in config.device_interfaces.items()}
    raw_ns = RawTransmitter(config.ns_interface)
    relay = TransparentRelay(config, ns_capture, device_capture, raw_device, raw_ns)
    relay._udp_sink = sink
    relay.run()


if __name__ == "__main__":
    main()
