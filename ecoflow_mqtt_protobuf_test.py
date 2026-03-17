#!/usr/bin/env python3
"""
EcoFlow MQTT Protobuf Mode-Switch Test (v2 — SHP3-specific)

Sends mode-change commands via MQTT using the exact protobuf structure
reverse-engineered from the EcoFlow Android app (jadx decompilation).

KEY FINDINGS (v2 corrections):
  - HR65 (SHP3) SN starts with "HR6", NOT "HR5" → Y2() returns false
  - This means dest=11 (not 2), and CfgPanelEnergyStrategyOperateMode (not CfgEnergyStrategy...)
  - DevAplComm.ConfigWrite field 544 = CfgPanelEnergyStrategyOperateMode
  - encType=0 for MQTT path (encType=1 was PD303-specific, not SHP3)
  - General DataBusEcoMqttProtocol.packetToBytes() always uses encType=0

Architecture:
  1. Build CfgPanelEnergyStrategyOperateMode (6 bool fields)
  2. Wrap in DevAplComm.ConfigWrite (field 544)
  3. Wrap in Common.Header with cmdSet=254, cmdId=17, dest=11, src=32, encType=0
  4. Wrap in Send_Header_Msg (field 1=msg)
  5. Publish raw bytes to /app/{userId}/{sn}/thing/property/set

Usage:
  python ecoflow_mqtt_protobuf_test.py [self_powered|backup|scheduled]
"""

import sys
import ssl
import time
import struct
import json
import random
import paho.mqtt.client as mqtt

# ──────────── Config ────────────
SN_HR65 = "HR65ZA1AVH7J0027"   # Gateway (Smart Home Panel 3)
SN_DPUX = "P101ZA1A9HA70164"   # Inverter (Delta Pro Ultra X)
SN = SN_HR65  # Default target; override with --sn=DPUX
USER_ID = "1971363830522871810"

MQTT_HOST = "mqtt-a.ecoflow.com"
MQTT_PORT = 8883
MQTT_USER = "app-740f41d44de04eaf83832f8a801252e9"
MQTT_PASS = "c1e46f17f6994a1e8252f1e1f3135b68"
CLIENT_ID = "ANDROID_696905537_1971363830522871810"

SET_TOPIC = f"/app/{USER_ID}/{SN}/thing/property/set"
SET_REPLY_TOPIC = f"/app/{USER_ID}/{SN}/thing/property/set_reply"
TELEMETRY_TOPIC = f"/app/device/property/{SN}"

# ──────────── Protobuf manual encoding ────────────
# We encode protobuf manually (no .proto compilation needed).
# Wire types: 0=varint, 1=64bit, 2=length-delimited, 5=32bit

def encode_varint(value):
    """Encode an unsigned integer as a protobuf varint."""
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)

def encode_field_varint(field_number, value, force=False):
    """Encode a varint field (wire type 0). If force=True, encode even if value is 0."""
    if value == 0 and not force:
        return b""  # protobuf default, omit
    tag = (field_number << 3) | 0  # wire type 0
    return encode_varint(tag) + encode_varint(value)

def encode_field_bool(field_number, value):
    """Encode a bool field (wire type 0, value 0 or 1)."""
    if not value:
        return b""  # false is default, omit
    tag = (field_number << 3) | 0
    return encode_varint(tag) + encode_varint(1)

def encode_field_bytes(field_number, data):
    """Encode a length-delimited field (wire type 2) with raw bytes."""
    tag = (field_number << 3) | 2
    return encode_varint(tag) + encode_varint(len(data)) + data

def encode_field_string(field_number, s):
    """Encode a string field (wire type 2)."""
    data = s.encode("utf-8")
    return encode_field_bytes(field_number, data)

def encode_field_message(field_number, message_bytes):
    """Encode a sub-message field (wire type 2)."""
    return encode_field_bytes(field_number, message_bytes)


# ──────────── Message builders ────────────

def build_cfg_panel_energy_strategy_operate_mode(self_powered=False, scheduled=False, tou=False,
                                                   eps_mode=False, mix_scheduled=False,
                                                   intelligent_schedule=False):
    """
    DevAplComm.CfgPanelEnergyStrategyOperateMode (for SHP3/HR65):
      field 1: operateSelfPoweredOpen (bool)
      field 2: operateScheduledOpen (bool)
      field 3: operateTouModeOpen (bool)
      field 4: operateEpsMode (bool)
      field 5: operateMixScheduledOpen (bool)
      field 6: operateIntelligentScheduleModeOpen (bool)

    Mode mapping (from app code):
      Self-Powered: field1=true, rest=false
      Backup: ALL false
      Scheduled Tasks: field2=true, rest=false
      TOU/AI: field3=true, rest=false
    """
    msg = b""
    msg += encode_field_bool(1, self_powered)
    msg += encode_field_bool(2, scheduled)
    msg += encode_field_bool(3, tou)
    msg += encode_field_bool(4, eps_mode)
    msg += encode_field_bool(5, mix_scheduled)
    msg += encode_field_bool(6, intelligent_schedule)
    return msg

def build_config_write_mode(cfg_panel_mode_bytes):
    """
    DevAplComm.ConfigWrite with mode change:
      field 544: cfgPanleEnergyStrategyOperateMode (message)
      (Note: "Panle" is a typo in the actual EcoFlow code)
    """
    return encode_field_message(544, cfg_panel_mode_bytes)

def build_config_write_charge(enable, channel=1, use_normal_chg=False):
    """
    Consumer grid charge control for SHP3/HR65.
    Uses BackupCtrl message on cfg_panel_backup_ch{N}_ctrl field.

    BackupCtrl:
      field 1: ctrlEn (int32) — AC switch status (1=enabled)
      field 2: ctrlForceChg (int32) — force charge (older firmware): 1=ON, 2=OFF
      field 3: ctrlNormalChg (int32) — normal charge (newer firmware, pdFirmVer > 33555020): 1=ON, 2=OFF

    ConfigWrite fields:
      field 535: cfg_panel_backup_ch1_ctrl (BackupCtrl)
      field 536: cfg_panel_backup_ch2_ctrl (BackupCtrl)
      field 537: cfg_panel_backup_ch3_ctrl (BackupCtrl)

    KEY FINDING: The app uses 1=ON, 2=OFF (NOT 0 for off!)
    The app also only sets ONE of ctrlForceChg/ctrlNormalChg based on firmware version, not both.
    """
    charge_val = 1 if enable else 2  # 1=ON, 2=OFF (from app reverse engineering)

    backup_ctrl = b""
    backup_ctrl += encode_field_varint(1, 1)  # ctrlEn = 1 (AC switch enabled)
    if use_normal_chg:
        backup_ctrl += encode_field_varint(3, charge_val)  # ctrlNormalChg (newer firmware)
    else:
        backup_ctrl += encode_field_varint(2, charge_val)  # ctrlForceChg (older/default firmware)

    # Field number depends on channel (535 for ch1, 536 for ch2, 537 for ch3)
    field_num = 534 + channel
    return encode_field_message(field_num, backup_ctrl)

def build_config_write_charge_power(watts, max_soc=None):
    """
    DevAplComm.ConfigWrite with charge power setting:
      field 542: cfgPanelMaxChargePowSet (uint32, watts)
      field 33:  cfgMaxChgSoc (uint32, percent 0-100, optional)
    """
    msg = b""
    msg += encode_field_varint(542, watts)
    if max_soc is not None:
        msg += encode_field_varint(33, max_soc)
    return msg

def build_header(pdata, seq):
    """
    Common.Header (from Common.java):
      field 1:  pdata (bytes) = DevAplComm.ConfigWrite serialized
      field 2:  src (int32) = 32 (Android app)
      field 3:  dest (int32) = 11 (SHP3/HR65, from C0() when Y2()=false)
      field 4:  dSrc (int32) = 1
      field 5:  dDest (int32) = 1
      field 6:  encType (int32) = 0 (no encryption for MQTT path)
      field 7:  checkType (int32) = 0 (omitted, default)
      field 8:  cmdFunc (int32) = 254 (GENERAL cmd set)
      field 9:  cmdId (int32) = 17 (write command)
      field 10: dataLen (int32) = len(pdata)
      field 11: needAck (int32) = 1
      field 14: seq (int32) = random
      field 15: productId (int32) = 1
      field 16: version (int32) = 19
      field 17: payloadVer (int32) = 1
      field 23: from (string) = "Android"
    """
    msg = b""
    msg += encode_field_bytes(1, pdata)           # pdata
    msg += encode_field_varint(2, 32)             # src (Android app)
    msg += encode_field_varint(3, 11)             # dest = 11 for SHP3/HR65 (C0() returns 11 when Y2()=false)
    msg += encode_field_varint(4, 1)              # dSrc
    msg += encode_field_varint(5, 1)              # dDest
    # field 6 encType = 0 (omitted, default) — DataBusEcoMqttProtocol uses encType=0 for MQTT
    # field 7 checkType = 0 (omitted, default)
    msg += encode_field_varint(8, 254)            # cmdFunc (cmdSet = GENERAL)
    msg += encode_field_varint(9, 17)             # cmdId (write command)
    msg += encode_field_varint(10, len(pdata))    # dataLen
    msg += encode_field_varint(11, 1)             # needAck
    msg += encode_field_varint(14, seq)           # seq
    msg += encode_field_varint(15, 1)             # productId
    msg += encode_field_varint(16, 19)            # version
    msg += encode_field_varint(17, 1)             # payloadVer
    msg += encode_field_string(23, "Android")     # from
    return msg

def build_send_header_msg(header_bytes):
    """
    Send_Header_Msg:
      field 1: msg (repeated Header message)
    """
    return encode_field_message(1, header_bytes)


# ──────────── MQTT callbacks ────────────
received_messages = []

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[MQTT] Connected successfully")
        client.subscribe(SET_REPLY_TOPIC, qos=1)
        client.subscribe(TELEMETRY_TOPIC, qos=1)
        print(f"[MQTT] Subscribed to {SET_REPLY_TOPIC}")
        print(f"[MQTT] Subscribed to {TELEMETRY_TOPIC}")
    else:
        print(f"[MQTT] Connection failed, rc={rc}")

def on_message(client, userdata, msg):
    ts = time.strftime("%H:%M:%S")
    topic = msg.topic
    payload = msg.payload

    if topic == SET_REPLY_TOPIC:
        print(f"\n[{ts}] === SET REPLY RECEIVED ===")
        print(f"  Topic: {topic}")
        print(f"  Payload ({len(payload)} bytes): {payload.hex()}")
        # Try to decode as protobuf
        try:
            decoded = decode_protobuf_simple(payload)
            print(f"  Decoded: {decoded}")
        except:
            pass
        received_messages.append(("set_reply", payload))
    elif topic == TELEMETRY_TOPIC:
        # Only print if it might be mode-related
        try:
            data = json.loads(payload)
            # Look for mode-related fields
            params = data.get("params", {})
            mode_keys = [k for k in params.keys() if 'mode' in k.lower() or 'operate' in k.lower() or 'self' in k.lower() or 'backup' in k.lower()]
            if mode_keys:
                print(f"\n[{ts}] TELEMETRY (mode-related):")
                for k in mode_keys:
                    print(f"  {k}: {params[k]}")
        except:
            # Binary telemetry
            if len(payload) < 200:
                print(f"\n[{ts}] TELEMETRY (binary, {len(payload)} bytes): {payload[:50].hex()}...")

def decode_protobuf_simple(data):
    """Simple protobuf decoder for debugging."""
    result = {}
    pos = 0
    while pos < len(data):
        if pos >= len(data):
            break
        # Read tag
        tag_byte = data[pos]
        field_number = tag_byte >> 3
        wire_type = tag_byte & 0x07
        pos += 1

        if wire_type == 0:  # varint
            value = 0
            shift = 0
            while pos < len(data):
                b = data[pos]
                pos += 1
                value |= (b & 0x7F) << shift
                shift += 7
                if (b & 0x80) == 0:
                    break
            result[f"field_{field_number}"] = value
        elif wire_type == 2:  # length-delimited
            length = 0
            shift = 0
            while pos < len(data):
                b = data[pos]
                pos += 1
                length |= (b & 0x7F) << shift
                shift += 7
                if (b & 0x80) == 0:
                    break
            value = data[pos:pos+length]
            pos += length
            try:
                result[f"field_{field_number}"] = value.decode("utf-8")
            except:
                result[f"field_{field_number}"] = f"bytes({len(value)}): {value.hex()[:40]}"
        elif wire_type == 1:  # 64-bit
            value = data[pos:pos+8]
            pos += 8
            result[f"field_{field_number}"] = f"64bit: {value.hex()}"
        elif wire_type == 5:  # 32-bit
            value = data[pos:pos+4]
            pos += 4
            result[f"field_{field_number}"] = f"32bit: {value.hex()}"
        else:
            result[f"field_{field_number}"] = f"unknown wire type {wire_type}"
            break
    return result


# ──────────── Main ────────────

def main():
    global SN, SET_TOPIC, SET_REPLY_TOPIC, TELEMETRY_TOPIC

    mode = "self_powered"
    if len(sys.argv) > 1:
        mode = sys.argv[1].lower().replace("-", "_")

    # Allow targeting DPUX with --sn=DPUX
    if len(sys.argv) > 2 and sys.argv[2].upper() == "DPUX":
        SN = SN_DPUX
        SET_TOPIC = f"/app/{USER_ID}/{SN}/thing/property/set"
        SET_REPLY_TOPIC = f"/app/{USER_ID}/{SN}/thing/property/set_reply"
        TELEMETRY_TOPIC = f"/app/device/property/{SN}"
        print(f"*** Targeting DPUX: {SN} ***")

    print("=" * 60)
    print("EcoFlow MQTT Protobuf Mode-Switch Test")
    print("=" * 60)

    # Build the command based on mode argument
    if mode == "self_powered":
        print(f"\nTarget: Self-Powered (battery discharges to home)")
        cfg = build_cfg_panel_energy_strategy_operate_mode(self_powered=True)
        config_write = build_config_write_mode(cfg)
    elif mode == "backup":
        print(f"\nTarget: Backup (battery idle, grid powers home)")
        cfg = build_cfg_panel_energy_strategy_operate_mode()  # all false
        config_write = build_config_write_mode(cfg)
    elif mode == "scheduled":
        print(f"\nTarget: Scheduled Tasks")
        cfg = build_cfg_panel_energy_strategy_operate_mode(scheduled=True)
        config_write = build_config_write_mode(cfg)
    elif mode == "tou":
        print(f"\nTarget: TOU/AI Mode")
        cfg = build_cfg_panel_energy_strategy_operate_mode(tou=True)
        config_write = build_config_write_mode(cfg)
    elif mode == "charge_on":
        print(f"\nTarget: Enable grid charging to battery (ctrlForceChg=1)")
        config_write = build_config_write_charge(True)
    elif mode == "charge_on_normal":
        print(f"\nTarget: Enable grid charging to battery (ctrlNormalChg=1, newer firmware)")
        config_write = build_config_write_charge(True, use_normal_chg=True)
    elif mode == "charge_off":
        print(f"\nTarget: Disable grid charging to battery (ctrlForceChg=2)")
        print(f"  KEY FIX: Using value 2 for OFF (not 0) — matches app behavior")
        config_write = build_config_write_charge(False)
    elif mode == "charge_off_normal":
        print(f"\nTarget: Disable grid charging to battery (ctrlNormalChg=2, newer firmware)")
        print(f"  KEY FIX: Using value 2 for OFF (not 0) — matches app behavior")
        config_write = build_config_write_charge(False, use_normal_chg=True)
    elif mode.startswith("charge_power_"):
        # Usage: charge_power_2400 or charge_power_2400_soc_90
        parts = mode.split("_")
        watts = int(parts[2])
        max_soc = None
        if "soc" in parts:
            soc_idx = parts.index("soc") + 1
            max_soc = int(parts[soc_idx])
        print(f"\nTarget: Set charge power to {watts}W" + (f", max SOC {max_soc}%" if max_soc else ""))
        config_write = build_config_write_charge_power(watts, max_soc)
    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python ecoflow_mqtt_protobuf_test.py <command>")
        print("  Mode commands:   self_powered | backup | scheduled | tou")
        print("  Charge commands: charge_on | charge_off | charge_on_normal | charge_off_normal")
        print("  Power commands:  charge_power_<watts> | charge_power_<watts>_soc_<percent>")
        print("  Examples:        charge_power_2400  charge_power_1500_soc_90")
        print("  Note: 'normal' variants use ctrlNormalChg (newer firmware) instead of ctrlForceChg")
        sys.exit(1)

    # Build the full protobuf chain
    seq = random.randint(100000, 999999)
    header = build_header(config_write, seq)
    send_msg = build_send_header_msg(header)

    print(f"\nProtobuf chain:")
    print(f"  ConfigWrite: {config_write.hex()}")
    print(f"  Header (seq={seq}): {header.hex()}")
    print(f"  Send_Header_Msg ({len(send_msg)} bytes): {send_msg.hex()}")
    print(f"\nMQTT topic: {SET_TOPIC}")

    # Connect to MQTT
    print(f"\nConnecting to {MQTT_HOST}:{MQTT_PORT}...")
    client = mqtt.Client(client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)
    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    # Wait for connection
    time.sleep(3)

    if not client.is_connected():
        print("[ERROR] Failed to connect to MQTT broker")
        client.loop_stop()
        sys.exit(1)

    # Send the command
    print(f"\n{'='*60}")
    print(f"SENDING MODE CHANGE: {mode}")
    print(f"{'='*60}")
    result = client.publish(SET_TOPIC, send_msg, qos=1)
    print(f"Publish result: rc={result.rc}, mid={result.mid}")

    # Wait for reply and telemetry
    print(f"\nWaiting 30 seconds for reply and telemetry changes...")
    for i in range(30):
        time.sleep(1)
        if i % 5 == 4:
            print(f"  ... {i+1}s elapsed, {len(received_messages)} replies received")

    print(f"\n{'='*60}")
    print(f"RESULTS")
    print(f"{'='*60}")
    print(f"Set replies received: {len([m for m in received_messages if m[0] == 'set_reply'])}")

    if not received_messages:
        print("\nNO REPLY received. The device may not have processed the command.")
        print("Possible issues:")
        print("  - Wrong SN (should this go to the DPUX SN instead?)")
        print("  - Wrong dest address (tried 2, might need different)")
        print("  - Wrong cmdSet/cmdId combination")
        print("  - encType=1 might require encryption we're not doing")
        print("  - The cloud may filter/block this command pattern")

    client.loop_stop()
    client.disconnect()
    print("\nDone.")
    input("\nPress Enter to exit...")

if __name__ == "__main__":
    main()
