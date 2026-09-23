#!/usr/bin/env python3
"""
namespace 侧多端口监听程序
监听 8882/UDP (ACK + 拓扑上报) 和 10009/UDP (查询响应)
循环打印端口号 + 原始报文内容
"""
import selectors
import socket
import struct
import ipaddress
import time

# ===================== 监听端口 =====================
LISTEN_PORTS = {8882: "ACK/拓扑上报", 10009: "查询响应"}

# 拓扑上报常量
INFO_TYPE_1 = 0x14
INFO_TYPE_2 = 0x33
INJECT_ACK_CMD = 0x13
TOPO_HEADER_LEN = 12
TOPO_COMMAND_LEN = 13


def hexdump(data: bytes, cols: int = 16) -> str:
    """格式化 hex dump"""
    lines = []
    for i in range(0, len(data), cols):
        chunk = data[i:i + cols]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {i:04x}  {hex_part:<{cols*3}}  {ascii_part}")
    return "\n".join(lines)


def parse_ack(data: bytes, addr) -> str:
    if len(data) == 2 and data[0] == INJECT_ACK_CMD:
        return f"加注ACK: {'✅ 成功' if data[1] == 0 else '❌ 失败'}"
    return None


def parse_topo(data: bytes, addr) -> str:
    if len(data) < TOPO_HEADER_LEN:
        return None
    info1 = data[0]
    info3 = data[3]
    if info1 != INFO_TYPE_1 or info3 != INFO_TYPE_2:
        return None

    node_ip = str(ipaddress.IPv4Address(data[6:10]))
    route_num = data[10]
    count = data[11]
    lines = [f"拓扑上报: 本机={node_ip}  邻居数={count}"]
    offset = TOPO_HEADER_LEN
    for i in range(count):
        if offset + TOPO_COMMAND_LEN > len(data):
            break
        target = str(ipaddress.IPv4Address(data[offset:offset + 4]))
        is_sync, snr, field = struct.unpack_from(">BII", data, offset + 4)
        status = "同步" if is_sync else "不同步"
        lines.append(f"  → {target}  {status}  SNR={snr}  场强={field}")
        offset += TOPO_COMMAND_LEN
    return "\n".join(lines)


def parse_query_resp(data: bytes, addr) -> str:
    expected = 11985
    if len(data) == expected:
        return f"查询响应: 完整 {len(data)} 字节 (310B头 + 440B CKL + 5863B XTL + 5372B ZZW)"
    return f"查询响应: 长度={len(data)} (预期={expected})"


def parse_packet(port: int, data: bytes, addr) -> str:
    """解析报文内容"""
    if port == 8882:
        result = parse_ack(data, addr)
        if result:
            return result
        result = parse_topo(data, addr)
        if result:
            return result
        return "未知报文"
    elif port == 10009:
        return parse_query_resp(data, addr)
    return ""


def main():
    sel = selectors.DefaultSelector()
    socks = {}

    for port, desc in LISTEN_PORTS.items():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        sock.setblocking(False)
        sel.register(sock, selectors.EVENT_READ, port)
        socks[port] = sock
        print(f"[监听] 0.0.0.0:{port}  ({desc})")

    print(f"\n{'='*70}")
    print("等待接收报文...\n")

    seq = 0
    while True:
        events = sel.select(timeout=1)
        for key, _ in events:
            port = key.data
            sock = key.fileobj
            try:
                data, addr = sock.recvfrom(65535)
            except BlockingIOError:
                continue

            seq += 1
            ts = time.strftime("%H:%M:%S")
            desc = LISTEN_PORTS.get(port, "未知")

            print(f"{'='*70}")
            print(f"[#{seq}] {ts}  ← {addr[0]}:{addr[1]}")
            print(f"端口: {port} ({desc})  长度: {len(data)} 字节")
            print(f"原始 hex: {data.hex()}")
            print(f"--- hex dump ---")
            print(hexdump(data[:256] if len(data) > 256 else data))
            if len(data) > 256:
                print(f"  ... (省略 {len(data) - 256} 字节)")

            parsed = parse_packet(port, data, addr)
            if parsed:
                print(f"--- 解析 ---")
                print(parsed)
            print()


if __name__ == "__main__":
    main()