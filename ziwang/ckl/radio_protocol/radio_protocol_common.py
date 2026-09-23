#!/usr/bin/env python3
import ipaddress
import re
import struct
from typing import Dict, Iterable


RADIO_PORT = 10001
INJECT_ACK_PORT = 8882
QUERY_RESPONSE_PORT = 10009
INJECT_ACK_CMD = 0x13

QUERY_MSG_TYPE = 0x0A
QUERY_SUPER_OPT = 0x01
QUERY_LEN = 157
RESPONSE_HEADER_LEN = 310

CKL_PARAM_LEN = 440
XTL_PARAM_LEN = 5863
ZZW_PARAM_LEN = 5372

SHORT_FMT_VERSION = 0x01
CKL_SHORT_LEN = 9
XTL_SHORT_LEN = 12
ZZW_SHORT_LEN = 12

DEVICE_PARAM_LEN = {
    "ckl": CKL_PARAM_LEN,
    "xtl": XTL_PARAM_LEN,
    "zzw": ZZW_PARAM_LEN,
}
SHORT_PARAM_LEN = {
    "ckl": CKL_SHORT_LEN,
    "xtl": XTL_SHORT_LEN,
    "zzw": ZZW_SHORT_LEN,
}
SHORT_FIELD_ORDER = {
    "ckl": (
        "nodeNo",
        "radioFreq",
        "radioPower",
        "workFreqMode",
        "radioRate",
        "hostPrime",
    ),
    "xtl": (
        "nodeNum",
        "radioPower",
        "freqType",
        "freq",
        "userRate",
        "currentChannelNo",
        "hostPrime",
    ),
    "zzw": (
        "nodeNum",
        "radioPower",
        "freqType",
        "freq",
        "userRate",
        "superiorNetNo",
        "hostPrime",
    ),
}
QUERY_EQUIP = {
    "ckl": 0x01,
    "xtl": 0x02,
    "zzw": 0x04,
}
RADIO_TYPE = {
    "ckl": 0x01,
    "xtl": 0x02,
    "zzw": 0x03,
}

QUERY_FRAG_EXT_MAGIC = b"QFR1"
QUERY_FRAG_EXT_LEN = 8
FRAG_MAGIC = b"FRAG"
FRAG_HEADER_LEN = 10
DEFAULT_MAX_FRAG_PAYLOAD = 1390


def query_response_len(device: str) -> int:
    return RESPONSE_HEADER_LEN + DEVICE_PARAM_LEN[device]


def short_ack_count(device: str) -> int:
    return len(SHORT_FIELD_ORDER[device])


def normalize_hex(text: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", text or "")


def _u8(buf: bytes, off: int) -> int:
    return buf[off]


def _u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def _u16_be(buf: bytes, off: int) -> int:
    return struct.unpack_from(">H", buf, off)[0]


def _i32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<i", buf, off)[0]


def _f32(buf: bytes, off: int) -> float:
    return struct.unpack_from("<f", buf, off)[0]


def _f32_be(buf: bytes, off: int) -> float:
    return struct.unpack_from(">f", buf, off)[0]


def _hex(buf: bytes, off: int, length: int) -> str:
    return bytes(buf[off:off + length]).hex()


def _ip(buf: bytes, off: int) -> str:
    return str(ipaddress.IPv4Address(bytes(buf[off:off + 4])))


def _put_u8(buf: bytearray, off: int, value) -> None:
    buf[off] = int(value, 0) & 0xFF if isinstance(value, str) else int(value) & 0xFF


def _put_u16(buf: bytearray, off: int, value) -> None:
    value = int(value, 0) if isinstance(value, str) else int(value)
    struct.pack_into("<H", buf, off, value & 0xFFFF)


def _put_u16_be(buf: bytearray, off: int, value) -> None:
    value = int(value, 0) if isinstance(value, str) else int(value)
    struct.pack_into(">H", buf, off, value & 0xFFFF)


def _put_i32(buf: bytearray, off: int, value) -> None:
    value = int(value, 0) if isinstance(value, str) else int(value)
    struct.pack_into("<i", buf, off, value)


def _put_f32_scaled(buf: bytearray, off: int, value) -> None:
    struct.pack_into("<f", buf, off, float(value) * 100000.0)


def _put_f32_be_scaled(buf: bytearray, off: int, value) -> None:
    struct.pack_into(">f", buf, off, float(value) * 100000.0)


def _put_ip(buf: bytearray, off: int, value) -> None:
    buf[off:off + 4] = ipaddress.IPv4Address(str(value)).packed


def _put_hex(buf: bytearray, off: int, length: int, value) -> None:
    raw = bytes.fromhex(normalize_hex(str(value)))
    if len(raw) > length:
        raise ValueError(f"hex field too long: {len(raw)} > {length}")
    buf[off:off + length] = raw.ljust(length, b"\x00")


def freq_mhz_from_scaled(raw_value: float) -> float:
    return raw_value / 100000.0


def parse_ckl_param(buf: bytes) -> Dict[str, object]:
    if len(buf) != CKL_PARAM_LEN:
        raise ValueError(f"CKL_PARAM length {len(buf)} != {CKL_PARAM_LEN}")
    return {
        "optType": _i32(buf, 8),
        "deviceID": _hex(buf, 12, 5),
        "nodeNo": _u8(buf, 17),
        "nodeSize": _u8(buf, 18),
        "hostPrime": _u8(buf, 19),
        "nodeIp": _ip(buf, 23),
        "roleType": _u8(buf, 27),
        "radioRate": _u8(buf, 28),
        "radioPower": _u8(buf, 29),
        "radioFreq": _u16(buf, 32),
        "radioRateSelf": _u8(buf, 34),
        "workFreqMode": _u8(buf, 35),
        "securityKey": _u8(buf, 177),
        "securityKeySwitch": _u8(buf, 178),
        "hopFreqTblNo": _u8(buf, 179),
        "hopFreqNetNo": _u8(buf, 180),
        "relayMode": _u16(buf, 181),
        "broadcast": _u8(buf, 183),
        "slotTbl": _hex(buf, 184, 256),
    }


def parse_channel_param(buf: bytes, off: int) -> Dict[str, object]:
    scaled_freq = _f32(buf, off + 2)
    return {
        "channelNo": _u8(buf, off),
        "freqType": _u8(buf, off + 1),
        "freqRaw": scaled_freq,
        "freq": freq_mhz_from_scaled(scaled_freq),
        "infoKey": _u8(buf, off + 6),
        "transformKey": _u8(buf, off + 7),
        "hopFreqTableNo": _u8(buf, off + 8),
        "hopFreqNetNo": _u8(buf, off + 9),
        "userRate": _u8(buf, off + 10),
        "radioPrime": _u8(buf, off + 11),
        "radioWorkWave": _u8(buf, off + 12),
        "security": _u8(buf, off + 13),
        "warMode": _u8(buf, off + 14),
    }


def parse_xtl_param(buf: bytes) -> Dict[str, object]:
    if len(buf) != XTL_PARAM_LEN:
        raise ValueError(f"XTL_PARAM length {len(buf)} != {XTL_PARAM_LEN}")
    return {
        "optType": _i32(buf, 8),
        "radioTime": _hex(buf, 12, 7),
        "macAddr": _u8(buf, 19),
        "ipAddr": _ip(buf, 20),
        "radioPower": _u8(buf, 24),
        "deviceID": _hex(buf, 27, 5),
        "superiorNetNo": _u8(buf, 32),
        "nodeNum": _u8(buf, 33),
        "softwareVersion": _hex(buf, 35, 8),
        "currentChannelNo": _u8(buf, 43),
        "channelParams": parse_channel_param(buf, 44),
        "radioSyncMode": _u8(buf, 1339),
        "timeSlotAlloc": _u8(buf, 1340),
        "radioFlowCtrlMsg": _u8(buf, 3184),
        "routerType": _u8(buf, 3314),
    }


def parse_zzw_param(buf: bytes) -> Dict[str, object]:
    if len(buf) != ZZW_PARAM_LEN:
        raise ValueError(f"ZZW_PARAM length {len(buf)} != {ZZW_PARAM_LEN}")
    return {
        "optType": _i32(buf, 8),
        "radioTime": _hex(buf, 12, 7),
        "macAddr": _u8(buf, 19),
        "ipAddr": _ip(buf, 20),
        "radioPower": _u8(buf, 24),
        "deviceID": _hex(buf, 27, 5),
        "superiorNetNo": _u8(buf, 32),
        "nodeNum": _u8(buf, 33),
        "softwareVersion": _hex(buf, 35, 8),
        "currentChannelNo": _u8(buf, 43),
        "channelParams": parse_channel_param(buf, 44),
        "radioSyncMode": _u8(buf, 1339),
        "timeSlotAlloc": _u8(buf, 1340),
        "radioFlowCtrlMsg": _u8(buf, 3184),
        "routerType": _u8(buf, 3314),
        "slotTblNum": _u8(buf, 5369),
    }


def parse_device_param(device: str, body: bytes) -> Dict[str, object]:
    if device == "ckl":
        return parse_ckl_param(body)
    if device == "xtl":
        return parse_xtl_param(body)
    if device == "zzw":
        return parse_zzw_param(body)
    raise ValueError(f"unsupported device: {device}")


def parse_full_response(data: bytes, device: str) -> Dict[str, object]:
    expected = query_response_len(device)
    if len(data) != expected:
        raise ValueError(f"query response length {len(data)} != {expected}")
    body = data[RESPONSE_HEADER_LEN:]
    return {
        "headerLen": RESPONSE_HEADER_LEN,
        "deviceParamLen": DEVICE_PARAM_LEN[device],
        device: parse_device_param(device, body),
    }


def default_ckl_param(cfg: Dict[str, str] = None, default_ip: str = "0.0.0.0") -> bytes:
    cfg = cfg or {}
    buf = bytearray(CKL_PARAM_LEN)
    _put_i32(buf, 8, int(cfg.get("optType", 1)))
    _put_u8(buf, 17, cfg.get("nodeNo", 1))
    _put_u8(buf, 18, cfg.get("nodeSize", 4))
    _put_u8(buf, 19, cfg.get("hostPrime", 0))
    _put_ip(buf, 23, cfg.get("businessIp", cfg.get("nodeIp", default_ip)))
    _put_u8(buf, 28, cfg.get("radioRate", 1))
    _put_u8(buf, 29, cfg.get("radioPower", 1))
    _put_u16(buf, 32, cfg.get("radioFreq", 1000))
    _put_u8(buf, 35, cfg.get("workFreqMode", 1))
    return bytes(buf)


def default_xtl_param(cfg: Dict[str, str] = None, default_ip: str = "0.0.0.0") -> bytes:
    cfg = cfg or {}
    buf = bytearray(XTL_PARAM_LEN)
    _put_i32(buf, 8, 1)
    _put_ip(buf, 20, cfg.get("businessIp", cfg.get("ipAddr", default_ip)))
    _put_u8(buf, 24, cfg.get("radioPower", 1))
    _put_u8(buf, 32, cfg.get("superiorNetNo", 0))
    _put_u8(buf, 33, cfg.get("nodeNum", 1))
    _put_u8(buf, 43, cfg.get("currentChannelNo", 0))
    _put_u8(buf, 44, cfg.get("channelNo", cfg.get("currentChannelNo", 0)))
    _put_u8(buf, 45, cfg.get("freqType", 1))
    _put_f32_scaled(buf, 46, cfg.get("freq", 300.0))
    _put_u8(buf, 54, cfg.get("userRate", 1))
    _put_u8(buf, 55, cfg.get("hostPrime", cfg.get("radioPrime", 0)))
    return bytes(buf)


def default_zzw_param(cfg: Dict[str, str] = None, default_ip: str = "0.0.0.0") -> bytes:
    cfg = cfg or {}
    buf = bytearray(ZZW_PARAM_LEN)
    _put_i32(buf, 8, 1)
    _put_ip(buf, 20, cfg.get("businessIp", cfg.get("ipAddr", default_ip)))
    _put_u8(buf, 24, cfg.get("radioPower", 1))
    _put_u8(buf, 32, cfg.get("superiorNetNo", 0))
    _put_u8(buf, 33, cfg.get("nodeNum", 1))
    _put_u8(buf, 43, cfg.get("currentChannelNo", 0))
    _put_u8(buf, 44, cfg.get("channelNo", cfg.get("currentChannelNo", 0)))
    _put_u8(buf, 45, cfg.get("freqType", 1))
    _put_f32_scaled(buf, 46, cfg.get("freq", 300.0))
    _put_u8(buf, 54, cfg.get("userRate", 1))
    _put_u8(buf, 55, cfg.get("hostPrime", cfg.get("radioPrime", 0)))
    return bytes(buf)


def default_device_param(device: str, cfg: Dict[str, str] = None, default_ip: str = "0.0.0.0") -> bytes:
    if device == "ckl":
        return default_ckl_param(cfg, default_ip)
    if device == "xtl":
        return default_xtl_param(cfg, default_ip)
    if device == "zzw":
        return default_zzw_param(cfg, default_ip)
    raise ValueError(f"unsupported device: {device}")


def build_full_query_response(current_device: str, current_param: bytes) -> bytes:
    if len(current_param) != DEVICE_PARAM_LEN[current_device]:
        raise ValueError(
            f"{current_device} param length {len(current_param)} != {DEVICE_PARAM_LEN[current_device]}"
        )
    return b"\x00" * RESPONSE_HEADER_LEN + current_param


def validate_short_header(device: str, body: bytes) -> None:
    if len(body) != SHORT_PARAM_LEN[device]:
        raise ValueError(f"{device} short parameter length {len(body)} != {SHORT_PARAM_LEN[device]}")
    if body[0] != SHORT_FMT_VERSION:
        raise ValueError(f"invalid fmtVersion 0x{body[0]:02x}")
    if body[1] != RADIO_TYPE[device]:
        raise ValueError(f"invalid radioType 0x{body[1]:02x} for {device}")


def parse_short_param(device: str, body: bytes) -> Dict[str, object]:
    validate_short_header(device, body)
    if device == "ckl":
        return {
            "fmtVersion": body[0],
            "radioType": body[1],
            "nodeNo": _u8(body, 2),
            "radioFreq": _u16_be(body, 3),
            "radioPower": _u8(body, 5),
            "workFreqMode": _u8(body, 6),
            "radioRate": _u8(body, 7),
            "hostPrime": _u8(body, 8),
        }
    if device == "xtl":
        raw_freq = _f32_be(body, 5)
        return {
            "fmtVersion": body[0],
            "radioType": body[1],
            "nodeNum": _u8(body, 2),
            "radioPower": _u8(body, 3),
            "freqType": _u8(body, 4),
            "freqRaw": raw_freq,
            "freq": freq_mhz_from_scaled(raw_freq),
            "userRate": _u8(body, 9),
            "currentChannelNo": _u8(body, 10),
            "hostPrime": _u8(body, 11),
        }
    raw_freq = _f32_be(body, 5)
    return {
        "fmtVersion": body[0],
        "radioType": body[1],
        "nodeNum": _u8(body, 2),
        "radioPower": _u8(body, 3),
        "freqType": _u8(body, 4),
        "freqRaw": raw_freq,
        "freq": freq_mhz_from_scaled(raw_freq),
        "userRate": _u8(body, 9),
        "superiorNetNo": _u8(body, 10),
        "hostPrime": _u8(body, 11),
    }


def apply_short_param(device: str, base_param: bytes, short_body: bytes,
                      business_ip: str = None) -> bytes:
    parsed = parse_short_param(device, short_body)
    buf = bytearray(base_param)
    if device == "ckl":
        _put_u8(buf, 17, parsed["nodeNo"])
        _put_u8(buf, 19, parsed["hostPrime"])
        _put_u8(buf, 28, parsed["radioRate"])
        _put_u8(buf, 29, parsed["radioPower"])
        _put_u16(buf, 32, parsed["radioFreq"])
        _put_u8(buf, 35, parsed["workFreqMode"])
        if business_ip:
            _put_ip(buf, 23, business_ip)
        return bytes(buf)
    if device == "xtl":
        _put_u8(buf, 24, parsed["radioPower"])
        _put_u8(buf, 33, parsed["nodeNum"])
        _put_u8(buf, 43, parsed["currentChannelNo"])
        _put_u8(buf, 44, parsed["currentChannelNo"])
        _put_u8(buf, 45, parsed["freqType"])
        struct.pack_into("<f", buf, 46, float(parsed["freqRaw"]))
        _put_u8(buf, 54, parsed["userRate"])
        _put_u8(buf, 55, parsed["hostPrime"])
        if business_ip:
            _put_ip(buf, 20, business_ip)
        return bytes(buf)
    _put_u8(buf, 24, parsed["radioPower"])
    _put_u8(buf, 32, parsed["superiorNetNo"])
    _put_u8(buf, 33, parsed["nodeNum"])
    _put_u8(buf, 43, 0)
    _put_u8(buf, 44, 0)
    _put_u8(buf, 45, parsed["freqType"])
    struct.pack_into("<f", buf, 46, float(parsed["freqRaw"]))
    _put_u8(buf, 54, parsed["userRate"])
    _put_u8(buf, 55, parsed["hostPrime"])
    if business_ip:
        _put_ip(buf, 20, business_ip)
    return bytes(buf)


SHORT_FIELD_WRITERS = {
    "ckl": {
        "nodeNo": (2, _put_u8),
        "radioFreq": (3, _put_u16_be),
        "radioPower": (5, _put_u8),
        "workFreqMode": (6, _put_u8),
        "radioRate": (7, _put_u8),
        "hostPrime": (8, _put_u8),
    },
    "xtl": {
        "nodeNum": (2, _put_u8),
        "radioPower": (3, _put_u8),
        "freqType": (4, _put_u8),
        "freq": (5, _put_f32_be_scaled),
        "userRate": (9, _put_u8),
        "currentChannelNo": (10, _put_u8),
        "hostPrime": (11, _put_u8),
        "radioPrime": (11, _put_u8),
    },
    "zzw": {
        "nodeNum": (2, _put_u8),
        "radioPower": (3, _put_u8),
        "freqType": (4, _put_u8),
        "freq": (5, _put_f32_be_scaled),
        "userRate": (9, _put_u8),
        "superiorNetNo": (10, _put_u8),
        "hostPrime": (11, _put_u8),
        "radioPrime": (11, _put_u8),
    },
}


def default_short_param(device: str) -> bytes:
    buf = bytearray(SHORT_PARAM_LEN[device])
    buf[0] = SHORT_FMT_VERSION
    buf[1] = RADIO_TYPE[device]
    if device == "ckl":
        _put_u8(buf, 2, 1)
        _put_u16_be(buf, 3, 1000)
        _put_u8(buf, 5, 1)
        _put_u8(buf, 6, 1)
        _put_u8(buf, 7, 1)
        _put_u8(buf, 8, 0)
    elif device == "xtl":
        _put_u8(buf, 2, 1)
        _put_u8(buf, 3, 1)
        _put_u8(buf, 4, 1)
        _put_f32_be_scaled(buf, 5, 300.0)
        _put_u8(buf, 9, 1)
        _put_u8(buf, 10, 0)
        _put_u8(buf, 11, 0)
    else:
        _put_u8(buf, 2, 1)
        _put_u8(buf, 3, 1)
        _put_u8(buf, 4, 1)
        _put_f32_be_scaled(buf, 5, 300.0)
        _put_u8(buf, 9, 1)
        _put_u8(buf, 10, 0)
        _put_u8(buf, 11, 0)
    return bytes(buf)


def build_short_injection(device: str, set_items: Iterable[str]) -> bytes:
    buf = bytearray(default_short_param(device))
    fields = SHORT_FIELD_WRITERS[device]
    for item in set_items or []:
        if "=" not in item:
            raise ValueError(f"invalid --set value: {item}")
        name, value = item.split("=", 1)
        field = fields.get(name)
        if field is None:
            raise ValueError(f"unknown {device} short injection field: {name}")
        offset, writer = field
        writer(buf, offset, value)
    return bytes(buf)


def split_short_injections(device: str, data: bytes):
    expected = SHORT_PARAM_LEN[device]
    if not data:
        raise ValueError("empty injection payload")
    if len(data) % expected != 0:
        raise ValueError(f"{device} injection payload length {len(data)} is not multiple of {expected}")
    return [data[i:i + expected] for i in range(0, len(data), expected)]


def parse_query_fragment_ext(data: bytes):
    if len(data) < QUERY_LEN + QUERY_FRAG_EXT_LEN:
        return False, 0, DEFAULT_MAX_FRAG_PAYLOAD
    if data[QUERY_LEN:QUERY_LEN + 4] != QUERY_FRAG_EXT_MAGIC:
        return False, 0, DEFAULT_MAX_FRAG_PAYLOAD
    tid, max_payload = struct.unpack_from("<HH", data, QUERY_LEN + 4)
    if max_payload < 256:
        max_payload = DEFAULT_MAX_FRAG_PAYLOAD
    return True, tid, min(max_payload, DEFAULT_MAX_FRAG_PAYLOAD)
