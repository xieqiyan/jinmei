#!/usr/bin/env python3
import argparse
import json
import os
import socket
import struct
import subprocess
import time
from typing import Dict

from radio_protocol_common import (
    DEFAULT_MAX_FRAG_PAYLOAD,
    DEVICE_PARAM_LEN,
    FRAG_MAGIC,
    INJECT_ACK_CMD,
    INJECT_ACK_PORT,
    QUERY_EQUIP,
    QUERY_MSG_TYPE,
    QUERY_RESPONSE_PORT,
    QUERY_SUPER_OPT,
    RADIO_PORT,
    SHORT_PARAM_LEN,
    apply_short_param,
    build_full_query_response,
    default_device_param,
    parse_device_param,
    parse_query_fragment_ext,
    parse_short_param,
    query_response_len,
    short_ack_count,
    split_short_injections,
)

SERVICE_DEVICE = "zzw"


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


def write_key_values(path: str, values: Dict[str, object], order) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        for key in order:
            file.write(f"{key}={values[key]}\n")
    os.replace(tmp_path, path)


def config_path(args: argparse.Namespace) -> str:
    if args.config_file:
        return args.config_file
    if args.device == "ckl":
        return args.ckl_config
    return os.path.join(args.config_dir, f"{args.device}_config.cfg")


def read_iface_ipv4(iface: str = "br0") -> str:
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", iface],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    for token in result.stdout.split():
        if "/" in token and token.count(".") == 3:
            return token.split("/", 1)[0]
    return ""


def resolve_default_ip(args: argparse.Namespace) -> str:
    cfg = read_key_values(config_path(args))
    for key in ("businessIp", "nodeIp", "ipAddr"):
        value = cfg.get(key, "")
        if value and value != "0.0.0.0":
            return value
    return read_iface_ipv4("br0") or "0.0.0.0"


def finalize_args(args: argparse.Namespace) -> argparse.Namespace:
    if not args.state:
        args.state = os.path.join(args.config_dir, f"{args.device}_params.json")
    if not args.default_ip:
        args.default_ip = resolve_default_ip(args)
    return args


def load_param(args: argparse.Namespace) -> bytes:
    fallback = default_device_param(args.device, read_key_values(config_path(args)), args.default_ip)
    if not os.path.exists(args.state):
        return fallback
    try:
        with open(args.state, "r", encoding="utf-8") as file:
            stored = json.load(file)
        raw = bytes.fromhex(stored.get("raw", ""))
        if len(raw) == DEVICE_PARAM_LEN[args.device]:
            return raw
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return fallback


def save_param(path: str, device: str, body: bytes, injection_raw: bytes = b"") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "updatedAt": int(time.time()),
        "device": device,
        "length": len(body),
        "summary": parse_device_param(device, body),
        "raw": body.hex(),
    }
    if injection_raw:
        payload["injectionRaw"] = injection_raw.hex()
        payload["injectionSummary"] = parse_short_param(device, injection_raw)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)
        file.write("\n")
    os.replace(tmp_path, path)


def sync_device_config(body: bytes, args: argparse.Namespace) -> None:
    path = config_path(args)
    parsed = parse_device_param(args.device, body)

    if args.device == "ckl":
        values = {
            "nodeNo": parsed["nodeNo"],
            "businessIp": parsed["nodeIp"],
            "radioFreq": parsed["radioFreq"],
            "radioPower": parsed["radioPower"],
            "workFreqMode": parsed["workFreqMode"],
            "radioRate": parsed["radioRate"],
            "hostPrime": parsed["hostPrime"],
        }
        write_key_values(path, values, (
            "nodeNo", "businessIp", "radioFreq", "radioPower",
            "workFreqMode", "radioRate", "hostPrime",
        ))
        return

    if args.device == "xtl":
        channel = parsed["channelParams"]
        values = {
            "nodeNum": parsed["nodeNum"],
            "businessIp": parsed["ipAddr"],
            "radioPower": parsed["radioPower"],
            "freqType": channel["freqType"],
            "freq": channel["freq"],
            "userRate": channel["userRate"],
            "currentChannelNo": parsed["currentChannelNo"],
            "hostPrime": channel["radioPrime"],
        }
        write_key_values(path, values, (
            "nodeNum", "businessIp", "radioPower", "freqType", "freq",
            "userRate", "currentChannelNo", "hostPrime",
        ))
        return

    channel = parsed["channelParams"]
    values = {
        "nodeNum": parsed["nodeNum"],
        "businessIp": parsed["ipAddr"],
        "radioPower": parsed["radioPower"],
        "freqType": channel["freqType"],
        "freq": channel["freq"],
        "userRate": channel["userRate"],
        "superiorNetNo": parsed["superiorNetNo"],
        "hostPrime": channel["radioPrime"],
    }
    write_key_values(path, values, (
        "nodeNum", "businessIp", "radioPower", "freqType", "freq",
        "userRate", "superiorNetNo", "hostPrime",
    ))


def is_query_packet(data: bytes) -> bool:
    return len(data) >= 3 and data[0] == QUERY_MSG_TYPE and data[1] == QUERY_SUPER_OPT


def query_matches_service(data: bytes, device: str) -> bool:
    return bool(data[2] & QUERY_EQUIP[device])


def send_fragmented_response(sock: socket.socket, full_data: bytes, dest_ip: str,
                             transaction_id: int, max_payload: int) -> int:
    max_payload = max(256, min(max_payload, DEFAULT_MAX_FRAG_PAYLOAD))
    total = (len(full_data) + max_payload - 1) // max_payload
    for idx in range(total):
        start = idx * max_payload
        payload = full_data[start:start + max_payload]
        header = struct.pack("<4sHHH", FRAG_MAGIC, transaction_id & 0xFFFF, total, idx)
        sock.sendto(header + payload, (dest_ip, QUERY_RESPONSE_PORT))
    return total


def apply_injections(current_device: str, data: bytes, current_body: bytes, peer_ip: str):
    try:
        records = split_short_injections(current_device, data)
    except ValueError as exc:
        return [(False, f"{current_device}_bad_short_payload_{exc}", current_body, b"", 1)]

    results = []
    body = current_body
    for idx, record in enumerate(records, 1):
        try:
            body = apply_short_param(current_device, body, record, peer_ip)
            parse_device_param(current_device, body)
        except (ValueError, struct.error) as exc:
            results.append((False, f"{current_device}_short_{idx}_parse_error_{exc}", body, record, 1))
            continue
        results.append((True, f"{current_device}_short_{idx}", body, record, short_ack_count(current_device)))
    return results


def serve(args: argparse.Namespace) -> None:
    body = load_param(args)
    sync_device_config(body, args)
    save_param(args.state, args.device, body)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, RADIO_PORT))
    print(
        f"radio_param_service[{args.device}] listening on {args.bind}:{RADIO_PORT}, "
        f"deviceParamLen={DEVICE_PARAM_LEN[args.device]}, shortParamLen={SHORT_PARAM_LEN[args.device]}, "
        f"queryResponseLen={query_response_len(args.device)}",
        flush=True,
    )

    while True:
        data, peer = sock.recvfrom(65535)
        peer_ip = peer[0]
        response_ip = args.response_ip or peer_ip

        if is_query_packet(data):
            if not query_matches_service(data, args.device):
                print(f"reject query optEquip=0x{data[2]:02x} on {args.device} service from {peer_ip}",
                      flush=True)
                continue
            response = build_full_query_response(args.device, body)
            wants_frag, tid, max_payload = parse_query_fragment_ext(data)
            if wants_frag:
                count = send_fragmented_response(sock, response, response_ip, tid, max_payload)
                print(
                    f"query {args.device} fragmented response to {response_ip} "
                    f"(requester {peer_ip}), "
                    f"len={len(response)}, fragments={count}",
                    flush=True,
                )
            else:
                sock.sendto(response, (response_ip, QUERY_RESPONSE_PORT))
                print(f"query {args.device} response to {response_ip} (requester {peer_ip}), len={len(response)}", flush=True)
            continue

        for ok, label, new_body, raw_record, ack_count in apply_injections(args.device, data, body, peer_ip):
            if ok:
                body = new_body
                sync_device_config(body, args)
                save_param(args.state, args.device, body, raw_record)
            result = 0 if ok else 1
            for ack_idx in range(ack_count):
                sock.sendto(bytes([INJECT_ACK_CMD, result]), (response_ip, INJECT_ACK_PORT))
                print(
                    f"inject {label} field_ack={ack_idx + 1}/{ack_count} "
                    f"from {peer_ip}, result={result}",
                    flush=True,
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=[SERVICE_DEVICE], default=SERVICE_DEVICE)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--state")
    parser.add_argument("--config-dir", default="/tmp/radio_test")
    parser.add_argument("--config-file")
    parser.add_argument("--ckl-config", default="/tmp/radio_test/ckl_config.cfg")
    parser.add_argument("--default-ip")
    parser.add_argument(
        "--response-ip",
        default=os.environ.get("RADIO_REPLY_IP"),
        help="IP destination for query responses and injection ACKs; requester IP remains used as businessIp",
    )
    return finalize_args(parser.parse_args())


if __name__ == "__main__":
    serve(parse_args())
