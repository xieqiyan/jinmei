#!/usr/bin/env python3
import argparse
import ipaddress
import json
import socket
import struct


TOPOLOGY_REPORT_PORT = 8882
INFO_TYPE_1 = 0x14
INFO_TYPE_2 = 0x33
HEADER_LEN = 12
COMMAND_LEN = 13


def parse_report(data: bytes) -> dict:
    if len(data) < HEADER_LEN:
        raise ValueError(f"topology report too short: {len(data)}")
    info1, length1, info2, length2 = struct.unpack_from(">BHBH", data, 0)
    if info1 != INFO_TYPE_1:
        raise ValueError(f"unexpected infoType1: 0x{info1:02x}")
    if info2 != INFO_TYPE_2:
        raise ValueError(f"unexpected infoType2: 0x{info2:02x}")
    node_ip = str(ipaddress.IPv4Address(data[6:10]))
    route_num = data[10]
    count = data[11]
    expected = HEADER_LEN + count * COMMAND_LEN
    expected_length2 = 6 + count * COMMAND_LEN
    expected_length1 = 1 + 2 + expected_length2
    if len(data) != expected:
        raise ValueError(f"topology length {len(data)} != expected {expected}")
    if length2 != expected_length2:
        raise ValueError(f"commandLength2 {length2} != expected {expected_length2}")
    if length1 != expected_length1:
        raise ValueError(f"commandLength1 {length1} != expected {expected_length1}")
    links = []
    offset = HEADER_LEN
    for _ in range(count):
        target = str(ipaddress.IPv4Address(data[offset:offset + 4]))
        is_sync, snr, field_intensity = struct.unpack_from(">BII", data, offset + 4)
        links.append({
            "targetAddress": target,
            "isSync": is_sync,
            "snr": float(snr),
            "fieldIntensity": float(field_intensity),
        })
        offset += COMMAND_LEN
    return {
        "infoType1": info1,
        "commandLength1": length1,
        "infoType2": info2,
        "commandLength2": length2,
        "nodeIP": node_ip,
        "routeNum": route_num,
        "chainPathNum": count,
        "links": links,
    }


def serve(args: argparse.Namespace) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.port))
    data, peer = sock.recvfrom(65535)
    parsed = parse_report(data)
    print(f"topology report from {peer[0]}:{peer[1]} len={len(data)}")
    print(json.dumps(parsed, indent=2, ensure_ascii=False))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=TOPOLOGY_REPORT_PORT)
    return parser.parse_args()


if __name__ == "__main__":
    serve(parse_args())
