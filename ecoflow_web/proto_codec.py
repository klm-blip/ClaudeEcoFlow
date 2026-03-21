"""
Protobuf decoder and SHP3 command encoder.
Confirmed working for EcoFlow Smart Home Panel 3 (HR65) + Delta Pro Ultra X.
"""

import random
import struct
from typing import Optional


# ─── Decoder ────────────────────────────────────────────────────────────────

class ProtoDecoder:
    @staticmethod
    def decode_varint(data: bytes, pos: int):
        result, shift = 0, 0
        while pos < len(data):
            b = data[pos]; pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        return result, pos

    @staticmethod
    def decode_message(data: bytes) -> dict:
        fields = {}
        pos = 0
        while pos < len(data):
            if pos >= len(data):
                break
            tag, pos = ProtoDecoder.decode_varint(data, pos)
            field_num = tag >> 3
            wire_type = tag & 0x07
            if field_num == 0:
                break
            if wire_type == 0:
                val, pos = ProtoDecoder.decode_varint(data, pos)
                fields[field_num] = val
            elif wire_type == 2:
                length, pos = ProtoDecoder.decode_varint(data, pos)
                raw = data[pos: pos + length]; pos += length
                nested = ProtoDecoder.decode_message(raw)
                fields[field_num] = {"_bytes": raw, "_nested": nested}
            elif wire_type == 5:
                val = struct.unpack_from("<f", data, pos)[0]; pos += 4
                fields[field_num] = val
            else:
                break
        return fields

    @staticmethod
    def get_float(fields: dict, *path: int) -> Optional[float]:
        node = fields
        for key in path[:-1]:
            entry = node.get(key)
            if not isinstance(entry, dict):
                return None
            node = entry.get("_nested", {})
        val = node.get(path[-1])
        if val is None:
            return None
        try:
            return float(val)
        except Exception:
            return None

    @staticmethod
    def get_int(fields: dict, *path: int) -> Optional[int]:
        v = ProtoDecoder.get_float(fields, *path)
        return int(v) if v is not None else None


# ─── Encoder primitives ────────────────────────────────────────────────────

def _encode_varint(value):
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)

def _encode_field_varint(field_number, value, force=False):
    if value == 0 and not force:
        return b""
    tag = (field_number << 3) | 0
    return _encode_varint(tag) + _encode_varint(value)

def _encode_field_bool(field_number, value):
    if not value:
        return b""
    tag = (field_number << 3) | 0
    return _encode_varint(tag) + _encode_varint(1)

def _encode_field_bytes(field_number, data):
    tag = (field_number << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(data)) + data

def _encode_field_string(field_number, s):
    return _encode_field_bytes(field_number, s.encode("utf-8"))

def _encode_field_message(field_number, message_bytes):
    return _encode_field_bytes(field_number, message_bytes)


# ─── SHP3 command builders ─────────────────────────────────────────────────
# DevAplComm.ConfigWrite → Common.Header → Send_Header_Msg

def build_mode_command(self_powered=False, scheduled=False, tou=False, eps_mode=None):
    """CfgPanelEnergyStrategyOperateMode on ConfigWrite field 544.

    Args:
        self_powered: Enable self-powered mode (battery discharges to home)
        scheduled: Enable scheduled mode
        tou: Enable TOU mode
        eps_mode: Enable EPS (20ms switchover). None = don't include field (preserve current).
                  True/False explicitly sends the field.
    """
    mode_msg = b""
    mode_msg += _encode_field_bool(1, self_powered)
    mode_msg += _encode_field_bool(2, scheduled)
    mode_msg += _encode_field_bool(3, tou)
    if eps_mode is not None:
        # Must use force=True so that eps_mode=False sends field 4=0 explicitly
        mode_msg += _encode_field_varint(4, 1 if eps_mode else 0, force=True)
    return _encode_field_message(544, mode_msg)


def build_eps_command(enable):
    """Standalone EPS toggle — sends only field 4 in ConfigWrite/544.
    Omits mode fields so current mode is preserved."""
    mode_msg = _encode_field_varint(4, 1 if enable else 0, force=True)
    return _encode_field_message(544, mode_msg)

def build_charge_command(enable, channel=1, use_normal_chg=False):
    """BackupCtrl on ConfigWrite field 535+channel. 1=ON, 2=OFF."""
    charge_val = 1 if enable else 2
    backup_ctrl = b""
    backup_ctrl += _encode_field_varint(1, 1)          # ctrlEn = 1
    if use_normal_chg:
        backup_ctrl += _encode_field_varint(3, charge_val)  # ctrlNormalChg
    else:
        backup_ctrl += _encode_field_varint(2, charge_val)  # ctrlForceChg
    field_num = 534 + channel
    return _encode_field_message(field_num, backup_ctrl)

def build_charge_power_command(watts, max_soc=None):
    """ConfigWrite field 542 (watts) + optional field 33 (SOC %)."""
    msg = b""
    msg += _encode_field_varint(542, watts)
    if max_soc is not None:
        msg += _encode_field_varint(33, max_soc)
    return msg

def build_header(pdata, seq):
    """Common.Header: dest=11, src=32, cmdSet=254, cmdId=17."""
    msg = b""
    msg += _encode_field_bytes(1, pdata)
    msg += _encode_field_varint(2, 32)        # src
    msg += _encode_field_varint(3, 11)        # dest (SHP3)
    msg += _encode_field_varint(4, 1)         # dSrc
    msg += _encode_field_varint(5, 1)         # dDest
    msg += _encode_field_varint(8, 254)       # cmdFunc
    msg += _encode_field_varint(9, 17)        # cmdId
    msg += _encode_field_varint(10, len(pdata))
    msg += _encode_field_varint(11, 1)        # needAck
    msg += _encode_field_varint(14, seq)
    msg += _encode_field_varint(15, 1)        # productId
    msg += _encode_field_varint(16, 19)       # version
    msg += _encode_field_varint(17, 1)        # payloadVer
    msg += _encode_field_string(23, "Android")
    return msg

def build_send_header_msg(header_bytes):
    """Outer wrapper: Send_Header_Msg field 1."""
    return _encode_field_message(1, header_bytes)

def build_and_wrap(config_write_bytes):
    """Full pipeline: ConfigWrite → Header → Send_Header_Msg. Returns ready-to-publish bytes."""
    seq = random.randint(100000, 999999)
    header = build_header(config_write_bytes, seq)
    return build_send_header_msg(header)
