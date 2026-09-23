#!/usr/bin/env python3
import argparse
import json
import os
import socket
import struct
import time
import xml.etree.ElementTree as ET

from radio_protocol_common import (
    DEFAULT_MAX_FRAG_PAYLOAD,
    FRAG_HEADER_LEN,
    FRAG_MAGIC,
    INJECT_ACK_CMD,
    INJECT_ACK_PORT,
    QUERY_EQUIP,
    QUERY_FRAG_EXT_MAGIC,
    QUERY_LEN,
    QUERY_RESPONSE_PORT,
    RADIO_PORT,
    SHORT_PARAM_LEN,
    build_short_injection,
    normalize_hex,
    parse_full_response,
    query_response_len,
    short_ack_count,
)


_query_tid = int(time.time()) & 0xFFFF


def _next_tid() -> int:
    global _query_tid
    _query_tid = (_query_tid + 1) & 0xFFFF
    return _query_tid


def device_kind(name: str) -> str:
    lower = (name or "").lower()
    if "ckl" in lower:
        return "ckl"
    if "xtl" in lower:
        return "xtl"
    if "zzw" in lower:
        return "zzw"
    return ""


def normalize_injection_hex(device: str, text: str) -> bytes:
    raw = bytes.fromhex(normalize_hex(text))
    expected = SHORT_PARAM_LEN[device]
    if len(raw) != expected:
        raise SystemExit(f"{device} short injection hex length must be {expected}, got {len(raw)}")
    return raw


def iter_xml_params(path: str, device: str, fa_id: str = None, devices=None):
    if not os.path.exists(path):
        raise SystemExit("请重新分发文件后再加注")
    tree = ET.parse(path)
    root = tree.getroot()
    if fa_id is not None and root.attrib.get("ID") != fa_id:
        raise SystemExit("参数文件已更新，请重新分发")

    wanted = set(devices or [])
    matched = False
    for dev in root.iter():
        if "设备" not in dev.tag:
            continue
        dev_id = dev.attrib.get("ID", "")
        name = dev.attrib.get("名称", "")
        kind = device_kind(name)
        if kind and kind != device:
            continue
        if wanted and dev_id not in wanted and name not in wanted and kind not in wanted:
            continue

        param = None
        for child in dev:
            if "参数" in child.tag:
                param = child
                break

        matched = True
        label = name or dev_id or device
        hex_text = normalize_hex(param.text if param is not None else "")
        if not hex_text:
            yield label, b""
            continue
        yield label, normalize_injection_hex(device, hex_text)

    if wanted and not matched:
        for item in sorted(wanted):
            yield item, b""


def build_injection(device: str, set_items) -> bytes:
    try:
        return build_short_injection(device, set_items or [])
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def recv_fragments(sock: socket.socket, expected_tid: int, timeout: float) -> bytes:
    fragments = {}
    total = None
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("timed out waiting for query fragments")
        sock.settimeout(remaining)
        data, _ = sock.recvfrom(65535)
        if len(data) < FRAG_HEADER_LEN:
            continue
        magic, tid, total_frag, idx = struct.unpack_from("<4sHHH", data, 0)
        if magic != FRAG_MAGIC or tid != expected_tid:
            continue
        if total is None:
            total = total_frag
        elif total != total_frag:
            continue
        if idx < total and idx not in fragments:
            fragments[idx] = data[FRAG_HEADER_LEN:]
        if total is not None and len(fragments) == total:
            result = bytearray()
            for i in range(total):
                result.extend(fragments[i])
            return bytes(result)


def selected_summary(parsed: dict, device: str, all_devices: bool) -> dict:
    return {
        "headerLen": parsed["headerLen"],
        "deviceParamLen": parsed["deviceParamLen"],
        device: parsed[device],
    }


def query(args: argparse.Namespace) -> None:
    tid = _next_tid()
    request = bytearray(QUERY_LEN)
    request[0] = 0x0A
    request[1] = 0x01
    request[2] = QUERY_EQUIP[args.device]
    if args.fragment:
        request.extend(QUERY_FRAG_EXT_MAGIC)
        request.extend(struct.pack("<HH", tid, args.max_frag_payload))

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", QUERY_RESPONSE_PORT))
        sock.settimeout(args.timeout)
        sock.sendto(request, (args.radio_ip, RADIO_PORT))

        if args.fragment:
            data = recv_fragments(sock, tid, args.timeout)
            peer = (args.radio_ip, QUERY_RESPONSE_PORT)
        else:
            data, peer = sock.recvfrom(65535)

    expected_len = query_response_len(args.device)
    print(f"response from {peer[0]}:{peer[1]} len={len(data)} expected={expected_len}")
    if len(data) != expected_len:
        raise SystemExit(f"invalid query response length: {len(data)}")
    if args.summary:
        parsed = parse_full_response(data, args.device)
        print(json.dumps(selected_summary(parsed, args.device, args.all), indent=2, ensure_ascii=False))
    if args.output:
        with open(args.output, "wb") as file:
            file.write(data)


def send_injection(radio_ip: str, label: str, payload: bytes, timeout: float) -> None:
    if not payload:
        print(f"inject {label}: empty parameter, skipped as failed")
        return
    device = label if label in SHORT_PARAM_LEN else "ckl"
    expected_acks = max(1, (len(payload) // SHORT_PARAM_LEN[device]) * short_ack_count(device))
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", INJECT_ACK_PORT))
        sock.sendto(payload, (radio_ip, RADIO_PORT))
        received = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("timed out waiting for injection ack")
            sock.settimeout(remaining)
            data, peer = sock.recvfrom(1024)
            if len(data) == 2 and data[0] == INJECT_ACK_CMD:
                received += 1
                result = "success" if data[1] == 0 else "failed"
                print(
                    f"inject {label}: ack {received}/{expected_acks} from "
                    f"{peer[0]}:{peer[1]} data={data.hex()} result={result}"
                )
                if received >= expected_acks:
                    return
                continue
            print(f"inject {label}: ignored non-ack packet from {peer[0]}:{peer[1]} data={data.hex()}")


def inject(args: argparse.Namespace) -> None:
    if args.xml:
        payloads = list(iter_xml_params(args.xml, args.device, args.fa_id, args.device_id))
    elif args.hex:
        payloads = [(args.device, normalize_injection_hex(args.device, args.hex))]
    else:
        payloads = [(args.device, build_injection(args.device, args.set or []))]

    for label, payload in payloads:
        send_injection(args.radio_ip, args.device, payload, args.timeout)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("query")
    q.add_argument("radio_ip")
    q.add_argument("device", choices=sorted(QUERY_EQUIP))
    q.add_argument("--timeout", type=float, default=10.0)
    q.add_argument("--fragment", action="store_true", default=False, help="enable non-protocol application fragmentation fallback")
    q.add_argument("--no-fragment", dest="fragment", action="store_false", help="keep strict protocol single UDP response mode (default)")
    q.add_argument("--max-frag-payload", type=int, default=DEFAULT_MAX_FRAG_PAYLOAD)
    q.add_argument("--summary", action="store_true")
    q.add_argument("--all", action="store_true", help="kept for compatibility; query returns current device only")
    q.add_argument("--output")
    q.set_defaults(func=query)

    inj = sub.add_parser("inject")
    inj.add_argument("radio_ip")
    inj.add_argument("device", choices=sorted(QUERY_EQUIP))
    inj.add_argument("--set", action="append")
    inj.add_argument("--hex")
    inj.add_argument("--xml")
    inj.add_argument("--fa-id")
    inj.add_argument("--device-id", action="append")
    inj.add_argument("--timeout", type=float, default=10.0)
    inj.set_defaults(func=inject)

    return parser.parse_args()


if __name__ == "__main__":
    options = parse_args()
    options.func(options)
