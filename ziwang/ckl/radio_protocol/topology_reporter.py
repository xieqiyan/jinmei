#!/usr/bin/env python3
import argparse
import ipaddress
import json
import os
import random
import socket
import struct
import time
from typing import Dict, List


TOPOLOGY_REPORT_PORT = 8882
INFO_TYPE_1 = 0x14
INFO_TYPE_2 = 0x33
ROUTE_NUM_CKL = 3
HEADER_LEN = 12
COMMAND_LEN = 13


def ip_to_bytes(value: str) -> bytes:
    return ipaddress.IPv4Address(value).packed


def read_key_values(path: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not path or not os.path.exists(path):
        return result
    with open(path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key] = value
    return result


def read_peer_status(path: str) -> Dict[str, object]:
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def read_status(peer_file: str, config_file: str) -> Dict[str, object]:
    if os.path.exists(peer_file):
        return read_peer_status(peer_file)
    cfg = read_key_values(config_file)
    return {
        "local": {
            "nodeNo": int(cfg.get("nodeNo", "0")),
            "businessIp": cfg.get("businessIp", "0.0.0.0"),
        },
        "peers": [],
    }


def metric_pair(reachable: bool) -> tuple:
    if reachable:
        return random.randint(22, 38), random.randint(65, 90)
    return random.randint(0, 8), random.randint(10, 35)


def build_report(status: Dict[str, object]) -> bytes:
    local = status.get("local") or {}
    peers = status.get("peers") or []
    local_ip = str(local.get("businessIp") or "0.0.0.0")

    commands: List[bytes] = []
    for peer in peers:
        target_ip = str(peer.get("businessIp") or "0.0.0.0")
        reachable = bool(peer.get("reachable"))
        snr, field_intensity = metric_pair(reachable)
        commands.append(
            ip_to_bytes(target_ip) +
            struct.pack(">BII", 1 if reachable else 0, snr, field_intensity)
        )

    chain_path_num = len(commands)
    body_len = 6 + COMMAND_LEN * chain_path_num
    first_len = 1 + 2 + body_len

    header = (
        struct.pack(">BHBH", INFO_TYPE_1, first_len, INFO_TYPE_2, body_len) +
        ip_to_bytes(local_ip) +
        struct.pack(">BB", ROUTE_NUM_CKL, chain_path_num)
    )
    return header + b"".join(commands)


def send_report(sock: socket.socket, data: bytes, dest_ip: str, dest_port: int) -> None:
    sock.sendto(data, (dest_ip, dest_port))


def resolve_dest_ip(args: argparse.Namespace, status: Dict[str, object]) -> str:
    if args.dest_ip:
        return args.dest_ip
    local = status.get("local") or {}
    dest_ip = str(local.get("businessIp") or "")
    if not dest_ip or dest_ip == "0.0.0.0":
        raise ValueError("dest-ip is not set and config businessIp is unavailable")
    return dest_ip


def serve(args: argparse.Namespace) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    last_error_log = 0.0
    print(
        f"ckl_topology_reporter started peer_file={args.peer_file} "
        f"dest={args.dest_ip or 'auto'}:{args.dest_port} interval={args.interval}",
        flush=True,
    )

    while True:
        try:
            status = read_status(args.peer_file, args.config_file)
            dest_ip = resolve_dest_ip(args, status)
            data = build_report(status)
            send_report(sock, data, dest_ip, args.dest_port)
            print(f"topology report sent len={len(data)} peers={data[11]} dest={dest_ip}", flush=True)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            now = time.monotonic()
            if now - last_error_log >= 5.0:
                print(f"topology report skipped: {exc}", flush=True)
                last_error_log = now
        time.sleep(args.interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--peer-file", default="/tmp/radio_test/peers.json")
    parser.add_argument("--config-file", default="/tmp/radio_test/ckl_config.cfg")
    parser.add_argument("--dest-ip")
    parser.add_argument("--dest-port", type=int, default=TOPOLOGY_REPORT_PORT)
    parser.add_argument("--interval", type=float, default=2.0)
    return parser.parse_args()


if __name__ == "__main__":
    serve(parse_args())
