"""Relay-to-device metadata envelope.

The outer UDP/IP headers identify the Relay/device hop. The original NS
address travels in this small application header so protocol services can
retain business identity without requiring Relay IP/MAC rewriting.
"""
from dataclasses import dataclass
import ipaddress
import struct
from typing import Optional

MAGIC = b"RLY1"
VERSION = 1
HEADER = struct.Struct("!4sBBBB4sHHH")
DEVICE_CODES = {"ckl": 1, "xtl": 2, "zzw": 3}
CODE_DEVICES = {value: key for key, value in DEVICE_CODES.items()}


@dataclass(frozen=True)
class RelayEnvelope:
    ns_ip: str
    device: str
    reply_port: int
    request_port: int
    payload: bytes


def encode(payload: bytes, ns_ip: str, device: str, reply_port: int,
           request_port: int = 10001) -> bytes:
    if device not in DEVICE_CODES:
        raise ValueError(f"unsupported device: {device}")
    if not 1 <= reply_port <= 65535 or not 1 <= request_port <= 65535:
        raise ValueError("ports must be in the range 1..65535")
    address = ipaddress.IPv4Address(ns_ip).packed
    header = HEADER.pack(
        MAGIC, VERSION, 0, DEVICE_CODES[device], 0, address,
        reply_port, len(payload), request_port,
    )
    return header + payload


def decode(data: bytes) -> Optional[RelayEnvelope]:
    if len(data) < HEADER.size or data[:4] != MAGIC:
        return None
    magic, version, _flags, device_code, _reserved, address, reply_port, payload_len, request_port = HEADER.unpack_from(data)
    if magic != MAGIC or version != VERSION or device_code not in CODE_DEVICES:
        raise ValueError("invalid relay envelope header")
    if len(data) != HEADER.size + payload_len:
        raise ValueError("relay envelope payload length mismatch")
    return RelayEnvelope(
        str(ipaddress.IPv4Address(address)),
        CODE_DEVICES[device_code],
        reply_port,
        request_port,
        data[HEADER.size:],
    )

