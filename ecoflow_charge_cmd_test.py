"""
EcoFlow Grid Charge Command Test
=================================
Tests grid charging ON/OFF commands decoded from MQTT sniffer captures.

== WHAT WAS DISCOVERED ==
The EcoFlow app sends charge commands as protobuf on the MQTT SET topic.
The key field is field127 inside a field1 sub-message of the inner payload.

Structure (fully reconstructed from sniffer hex):
  outer.field1 = inner_msg (100 bytes for ON, 108 bytes for OFF)
    inner_msg.field1   = sub_msg { field127 = pdata }
    inner_msg.field2   = 32
    inner_msg.field3   = 11
    inner_msg.field4   = 1
    inner_msg.field5   = 1
    inner_msg.field8   = 254
    inner_msg.field9   = 17  (charge commands; heartbeat uses 19)
    inner_msg.field10  = len(pdata) + 3  (size of field127 wrapper)
    inner_msg.field11  = 1
    inner_msg.field14  = seq  (incrementing)
    inner_msg.field15  = 96
    inner_msg.field16  = 4
    inner_msg.field17  = 1
    inner_msg.field23  = "Android"
    inner_msg.field26  = SN  (device serial number)
    inner_msg.field27  = SN  (repeated)

pdata for CHARGE ON (20 bytes):
    field2  = 1    (chargeEnable=ON)
    field5  = 1
    field6  = 127  (SOC limit / max charge)
    field7  = <nonce/timestamp>
    field10 = sub_msg { field1=4, field2=float(watts) }

pdata for CHARGE OFF (28 bytes):
    field1  = 0xFFFFFFFF  (-1, chargeEnable=OFF/disable)
    field2  = 1
    field3  = 1
    field5  = 1
    field6  = 127
    field7  = <nonce/timestamp>
    field10 = sub_msg { field1=4, field2=float(watts) }

== REFERENCE CAPTURES (from sniffer at 19:52:54 and 19:53:10) ==
Charge ON pdata (exact, 20 bytes):
    10 01 28 01 30 7f 38 b4 81 a8 09 52 07 08 04 15 00 00 a0 42
    (field7=19529908, watts=80.0W)

Charge OFF pdata (exact, 28 bytes):
    08 ff ff ff ff 0f 10 01 18 01 28 01 30 7f 38 aa 89 98 26 52 07 08 04 15 00 00 c8 42
    (field7=80086186, watts=100.0W)

Run:
    python ecoflow_charge_cmd_test.py
"""

import json
import os
import struct
import time
import threading
import paho.mqtt.client as mqtt

# ── Credentials ───────────────────────────────────────────────────────────────
_dir = os.path.dirname(os.path.abspath(__file__))

def _load_creds():
    creds = {
        "MQTT_USER": "app-740f41d44de04eaf83832f8a801252e9",
        "MQTT_PASS": "c1e46f17f6994a1e8252f1e1f3135b68",
        "CLIENT_ID": "ANDROID_696905537_1971363830522871810",
    }
    cred_file = os.path.join(_dir, "ecoflow_credentials.txt")
    if os.path.exists(cred_file):
        for line in open(cred_file).read().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k in creds:
                    creds[k] = v
    return creds

creds      = _load_creds()
MQTT_USER  = creds["MQTT_USER"]
MQTT_PASS  = creds["MQTT_PASS"]
BASE_ID    = creds["CLIENT_ID"]

_parts     = BASE_ID.split("_", 2)
SESSION_ID = _parts[2] if len(_parts) >= 3 else _parts[-1]
_rand      = _parts[1] if len(_parts) >= 3 else "696905537"
# Use a different CLIENT_ID to avoid collision with phone app
TEST_CLIENT_ID = f"ANDROID_{int(_rand)+7}_{SESSION_ID}"

SN        = "HR65ZA1AVH7J0027"
MQTT_HOST = "mqtt.ecoflow.com"
MQTT_PORT = 8883

SET_TOPIC       = f"/app/{SESSION_ID}/{SN}/thing/property/set"
TELEMETRY_TOPIC = f"/app/device/property/{SN}"
GET_TOPIC       = f"/app/{SESSION_ID}/{SN}/thing/property/get"
GET_TRIGGER     = json.dumps({
    "from": "HomeAssistant", "id": "999", "version": "1.1",
    "moduleType": 0, "operateType": "latestQuotas", "params": {}
})

# ── Protobuf encoder ──────────────────────────────────────────────────────────

def _varint(v):
    v = v & 0xFFFFFFFFFFFFFFFF
    out = []
    while True:
        out.append(v & 0x7F)
        v >>= 7
        if v == 0:
            break
    for i in range(len(out) - 1):
        out[i] |= 0x80
    return bytes(out)

def _pb(num, wire, val):
    tag = _varint((num << 3) | wire)
    if wire == 0:
        return tag + _varint(val)
    if wire == 2:
        return tag + _varint(len(val)) + val
    if wire == 5:
        return tag + struct.pack('<f', val)
    raise ValueError(f"Unknown wire type {wire}")


def build_charge_on_pdata(watts: float = 500.0) -> bytes:
    """
    pdata for grid charge ON.
    Decoded from sniffer capture at 19:52:54 (102-byte SET message).
    """
    sub = _pb(1, 0, 4) + _pb(2, 5, watts)   # sub-msg: field1=4, field2=float(watts)
    return (
        _pb(2,  0, 1)   +   # chargeEnable = 1 (ON)
        _pb(5,  0, 1)   +   # f5 = 1
        _pb(6,  0, 127) +   # f6 = 127 (SOC/charge limit)
        _pb(7,  0, int(time.time()) % (1 << 28)) +  # nonce/timestamp
        _pb(10, 2, sub)     # f10 = sub-msg {f1=4, f2=watts}
    )


def build_charge_off_pdata(watts: float = 500.0) -> bytes:
    """
    pdata for grid charge OFF.
    Decoded from sniffer capture at 19:53:10 (110-byte SET message).
    """
    sub = _pb(1, 0, 4) + _pb(2, 5, watts)
    return (
        _pb(1,  0, 0xFFFFFFFF) +  # chargeEnable = -1 (OFF/disable)
        _pb(2,  0, 1)          +
        _pb(3,  0, 1)          +
        _pb(5,  0, 1)          +
        _pb(6,  0, 127)        +
        _pb(7,  0, int(time.time()) % (1 << 28)) +
        _pb(10, 2, sub)
    )


def build_charge_command(pdata: bytes, seq: int, sn: str = SN) -> bytes:
    """
    Wrap pdata in the full command envelope as captured from EcoFlow app.
    Verified to produce exactly 102 bytes (ON) or 110 bytes (OFF).
    """
    f127  = _pb(127, 2, pdata)          # field127 wrapper = 2+1+len(pdata) bytes
    inner = (
        _pb(1,  2, f127)            +   # field1  = sub-msg {field127=pdata}
        _pb(2,  0, 32)              +   # field2  = 32
        _pb(3,  0, 11)              +   # field3  = 11
        _pb(4,  0, 1)               +   # field4  = 1
        _pb(5,  0, 1)               +   # field5  = 1
        _pb(8,  0, 254)             +   # field8  = 254
        _pb(9,  0, 17)              +   # field9  = 17  (charge commands)
        _pb(10, 0, len(pdata) + 3)  +   # field10 = len(f127)
        _pb(11, 0, 1)               +   # field11 = 1
        _pb(14, 0, seq)             +   # field14 = seq (incrementing)
        _pb(15, 0, 96)              +   # field15 = 96
        _pb(16, 0, 4)               +   # field16 = 4
        _pb(17, 0, 1)               +   # field17 = 1
        _pb(23, 2, b"Android")      +   # field23 = "Android"
        _pb(26, 2, sn.encode())     +   # field26 = SN
        _pb(27, 2, sn.encode())         # field27 = SN (repeated, as in app captures)
    )
    return _pb(1, 2, inner)


# ── Telemetry parsing (reused from working code) ──────────────────────────────
def decode_varint(data, pos):
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def parse_pdata(raw):
    out = {}
    pos = 0
    while pos < len(raw):
        try:
            tag, pos = decode_varint(raw, pos)
            fn = tag >> 3; wt = tag & 0x07
            if wt == 0:
                v, pos = decode_varint(raw, pos)
                out[fn] = ("int", v)
            elif wt == 2:
                ln, pos = decode_varint(raw, pos)
                chunk = raw[pos:pos+ln]; pos += ln
                if fn == 518 and ln == 4:
                    out[fn] = ("float", struct.unpack("<f", chunk)[0])
                else:
                    out[fn] = ("bytes", chunk)
            elif wt == 5:
                v = struct.unpack_from("<f", raw, pos)[0]; pos += 4
                out[fn] = ("float", v)
            else:
                break
        except Exception:
            break
    return out


def format_telemetry(fields):
    lines = []
    if 518 in fields and fields[518][0] == "float":
        lines.append(f"  Battery watts : {fields[518][1]:+.1f} W")
    if 1544 in fields:
        lines.append(f"  Home load     : {fields[1544][1]} W")
    if 515 in fields:
        lines.append(f"  Grid draw     : {fields[515][1]:.1f} W")
    if 1009 in fields and fields[1009][0] == "bytes":
        sub = fields[1009][1]
        sf  = parse_pdata(sub)
        if 4 in sf:
            mode_map = {1: "BACKUP/CHARGING", 2: "SELF-POWERED"}
            lines.append(f"  Mode          : {mode_map.get(sf[4][1], sf[4][1])}")
        if 5 in sf:
            lines.append(f"  SOC           : {sf[5][1]}%")
    return "\n".join(lines) if lines else None


# ── MQTT watcher ──────────────────────────────────────────────────────────────
mqtt_connected = threading.Event()
telemetry_log  = []
telemetry_lock = threading.Lock()

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"  MQTT connected as {TEST_CLIENT_ID}")
        client.subscribe(TELEMETRY_TOPIC, qos=0)
        mqtt_connected.set()
    else:
        print(f"  MQTT connect failed rc={rc}")

def on_message(client, userdata, msg):
    payload = msg.payload
    if len(payload) < 20:
        return
    try:
        tag, pos = decode_varint(payload, 0)
        if (tag >> 3) == 1 and (tag & 7) == 2:
            ln, pos = decode_varint(payload, pos)
            inner = payload[pos:pos+ln]
            tag2, p2 = decode_varint(inner, 0)
            if (tag2 >> 3) == 1 and (tag2 & 7) == 2:
                ln2, p2 = decode_varint(inner, p2)
                pdata = inner[p2:p2+ln2]
                fields = parse_pdata(pdata)
                ts = time.strftime("%H:%M:%S")
                summary = format_telemetry(fields)
                if summary:
                    with telemetry_lock:
                        telemetry_log.append((ts, summary))
                    print(f"\n[{ts}] TELEMETRY:\n{summary}")
    except Exception:
        pass

def start_mqtt():
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                             client_id=TEST_CLIENT_ID, protocol=mqtt.MQTTv311)
    except AttributeError:
        client = mqtt.Client(client_id=TEST_CLIENT_ID, protocol=mqtt.MQTTv311)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client


def trigger_telemetry(client):
    client.publish(GET_TOPIC, GET_TRIGGER, qos=1)
    time.sleep(3)


def send_command(client, payload_bytes, label):
    rc, mid = client.publish(SET_TOPIC, payload_bytes, qos=1)
    status  = "OK" if rc == 0 else f"FAIL rc={rc}"
    print(f"  Published [{status}]  mid={mid}  len={len(payload_bytes)} bytes")
    print(f"  HEX: {payload_bytes.hex()}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    WATTS = 500.0   # Test charge rate (Watts) — adjust as desired

    print("=" * 65)
    print("EcoFlow Grid Charge Command Test")
    print(f"SN       : {SN}")
    print(f"ClientID : {TEST_CLIENT_ID}")
    print(f"SET topic: {SET_TOPIC}")
    print(f"Test watts: {WATTS}W")
    print("=" * 65)

    # Verify command sizes before connecting
    pdata_on  = build_charge_on_pdata(WATTS)
    pdata_off = build_charge_off_pdata(WATTS)
    cmd_on    = build_charge_command(pdata_on,  seq=200)
    cmd_off   = build_charge_command(pdata_off, seq=201)

    print(f"\nCommand sizes:")
    print(f"  Charge ON  pdata={len(pdata_on)}b  total={len(cmd_on)}b  (expected 20b / 102b)")
    print(f"  Charge OFF pdata={len(pdata_off)}b  total={len(cmd_off)}b  (expected 28b / 110b)")

    if len(cmd_on) != 102 or len(cmd_off) != 110:
        print("  WARNING: unexpected sizes — check encoder!")
    else:
        print("  Sizes match sniffer captures exactly ✓")

    # Connect MQTT
    print("\nConnecting to MQTT...")
    mqtt_client = start_mqtt()
    if not mqtt_connected.wait(timeout=10):
        print("  WARNING: MQTT connect timed out — continuing anyway")

    # Baseline
    print("\n[BASELINE] Requesting current device state...")
    trigger_telemetry(mqtt_client)
    time.sleep(5)

    # ── TEST 1: Charge ON ────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print(f"TEST 1: Send CHARGE ON  (field127 pdata, field2=1, {WATTS}W)")
    print("=" * 65)
    seq = 200
    pdata = build_charge_on_pdata(WATTS)
    cmd   = build_charge_command(pdata, seq=seq)
    print(f"\n  Sending charge ON command (seq={seq})...")
    send_command(mqtt_client, cmd, "CHARGE ON")

    print("\n  Watching telemetry for 25s (look for Battery watts going POSITIVE)...")
    trigger_telemetry(mqtt_client)
    time.sleep(25)

    # ── TEST 2: Charge OFF ───────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print(f"TEST 2: Send CHARGE OFF  (field127 pdata, field1=-1, {WATTS}W)")
    print("=" * 65)
    seq = 201
    pdata = build_charge_off_pdata(WATTS)
    cmd   = build_charge_command(pdata, seq=seq)
    print(f"\n  Sending charge OFF command (seq={seq})...")
    send_command(mqtt_client, cmd, "CHARGE OFF")

    print("\n  Watching telemetry for 25s (look for Battery watts dropping to 0)...")
    trigger_telemetry(mqtt_client)
    time.sleep(25)

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("DONE. Telemetry changes observed:")
    with telemetry_lock:
        if telemetry_log:
            for ts, summary in telemetry_log:
                print(f"\n  [{ts}]")
                print(summary)
        else:
            print("  (no telemetry with decoded key fields)")
    print("=" * 65)
    mqtt_client.loop_stop()


if __name__ == "__main__":
    main()
