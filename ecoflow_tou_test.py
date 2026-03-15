"""
EcoFlow TOU Mode Test
=====================
Tests the notify-mode-changed REST endpoint discovered via HTTP Toolkit.

Endpoint: POST https://api-a.ecoflow.com/tou-service/goe/ai-mode/notify-mode-changed
Auth:      token header (session token from app)

Simultaneously monitors MQTT telemetry so we can see if the device
actually changes behavior when we call this endpoint.

targetMode values observed from app:
   2  = (unknown - possibly Self-Powered / charge-from-grid enabled)
  -1  = (unknown - possibly Off / default)
   0  = (guessing Backup mode - needs testing)
   1  = (guessing Self-Powered mode - needs testing)

Run:
  python ecoflow_tou_test.py
"""

import json
import os
import struct
import time
import threading
import requests
import paho.mqtt.client as mqtt

# ── Credentials ───────────────────────────────────────────────────────────────
_dir = os.path.dirname(os.path.abspath(__file__))

# REST session token captured from HTTP Toolkit
REST_TOKEN = "638e51c7c4c00e2842fcd91537b2a579903dac973848eeae56ec7dd25285553e"
REST_HOST  = "https://api-a.ecoflow.com"

# MQTT credentials (from ecoflow_credentials.txt if present)
def _load_creds():
    creds = {
        "MQTT_USER": "app-740f41d44de04eaf83832f8a801252e9",
        "MQTT_PASS": "c1e46f17f6994a1e8252f1e1f3135b68",
        "CLIENT_ID": "ANDROID_574080605_1971363830522871810",
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
_rand      = _parts[1] if len(_parts) >= 3 else "574080605"
TEST_ID    = f"ANDROID_{int(_rand)+3}_{SESSION_ID}"   # +3 to avoid collision

SN        = "HR65ZA1AVH7J0027"
MQTT_HOST = "mqtt.ecoflow.com"
MQTT_PORT = 8883

TELEMETRY_TOPIC = f"/app/device/property/{SN}"
GET_TOPIC       = f"/app/{SESSION_ID}/{SN}/thing/property/get"
GET_TRIGGER     = json.dumps({
    "from": "HomeAssistant", "id": "999", "version": "1.1",
    "moduleType": 0, "operateType": "latestQuotas", "params": {}
})

# ── Telemetry parsing ─────────────────────────────────────────────────────────
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
    """Parse key fields from pdata protobuf. Returns dict of interesting values."""
    out = {}
    pos = 0
    while pos < len(raw):
        try:
            tag, pos = decode_varint(raw, pos)
            fn  = tag >> 3
            wt  = tag & 0x07
            if wt == 0:
                v, pos = decode_varint(raw, pos)
                out[fn] = ("int", v)
            elif wt == 2:
                ln, pos = decode_varint(raw, pos)
                chunk = raw[pos:pos+ln]; pos += ln
                # field 518 = battery watts (float32, NOT a nested msg)
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
    # Battery watts
    if 518 in fields and fields[518][0] == "float":
        lines.append(f"  Battery watts : {fields[518][1]:+.1f} W")
    # Home load
    if 1544 in fields:
        lines.append(f"  Home load     : {fields[1544][1]} W")
    # Grid draw
    if 515 in fields:
        lines.append(f"  Grid draw     : {fields[515][1]:.1f} W")
    # mode / SOC from sub-msg at 1009
    if 1009 in fields and fields[1009][0] == "bytes":
        sub = fields[1009][1]
        sf  = parse_pdata(sub)
        if 4 in sf:
            mode_map = {1: "BACKUP/CHARGING", 2: "SELF-POWERED"}
            mode_val = sf[4][1]
            lines.append(f"  Mode          : {mode_map.get(mode_val, mode_val)}")
        if 5 in sf:
            lines.append(f"  SOC           : {sf[5][1]}%")
    return "\n".join(lines) if lines else "  (no key fields decoded)"

# ── MQTT watcher ──────────────────────────────────────────────────────────────
mqtt_connected = threading.Event()
telemetry_log  = []
telemetry_lock = threading.Lock()

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"  MQTT connected as {TEST_ID}")
        client.subscribe(TELEMETRY_TOPIC, qos=0)
        mqtt_connected.set()
    else:
        print(f"  MQTT connect failed rc={rc}")

def on_message(client, userdata, msg):
    payload = msg.payload
    # Skip short messages
    if len(payload) < 20:
        return
    # Try to extract pdata (field 1 of outer wrapper)
    try:
        tag, pos = decode_varint(payload, 0)
        if (tag >> 3) == 1 and (tag & 7) == 2:
            ln, pos = decode_varint(payload, pos)
            inner = payload[pos:pos+ln]
            # inner field 1 = pdata
            tag2, p2 = decode_varint(inner, 0)
            if (tag2 >> 3) == 1 and (tag2 & 7) == 2:
                ln2, p2 = decode_varint(inner, p2)
                pdata = inner[p2:p2+ln2]
                fields = parse_pdata(pdata)
                ts = time.strftime("%H:%M:%S")
                summary = format_telemetry(fields)
                if summary.strip():
                    with telemetry_lock:
                        telemetry_log.append((ts, summary))
                    print(f"\n[{ts}] TELEMETRY:\n{summary}")
    except Exception:
        pass

def start_mqtt():
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                             client_id=TEST_ID, protocol=mqtt.MQTTv311)
    except AttributeError:
        client = mqtt.Client(client_id=TEST_ID, protocol=mqtt.MQTTv311)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client

# ── REST caller ───────────────────────────────────────────────────────────────
def notify_mode(target_mode: int) -> dict:
    url = f"{REST_HOST}/tou-service/goe/ai-mode/notify-mode-changed"
    headers = {
        "token":        REST_TOKEN,
        "Content-Type": "application/json",
        "lang":         "en",
        "User-Agent":   "EcoFlow/6.11.0.1731 (Android 16)",
    }
    body = {"sn": SN, "systemNo": "", "targetMode": target_mode}
    print(f"\n  POST {url}")
    print(f"  Body: {json.dumps(body)}")
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=15)
        print(f"  HTTP {resp.status_code}  ({len(resp.content)} bytes)")
        print(f"  Response: {resp.text}")
        return resp.json() if resp.ok else {}
    except Exception as e:
        print(f"  ERROR: {e}")
        return {}

def trigger_telemetry(client):
    print(f"\n  Requesting telemetry (GET trigger)...")
    client.publish(GET_TOPIC, GET_TRIGGER, qos=1)
    time.sleep(3)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 65)
    print("EcoFlow TOU Mode Test")
    print(f"SN: {SN}")
    print(f"REST: {REST_HOST}")
    print(f"Token: {REST_TOKEN[:16]}...")
    print("=" * 65)

    # Start MQTT watcher
    print("\nConnecting to MQTT (to watch for telemetry changes)...")
    mqtt_client = start_mqtt()
    if not mqtt_connected.wait(timeout=10):
        print("  WARNING: MQTT connect timed out — continuing anyway")

    # Get baseline telemetry
    print("\n[BASELINE] Requesting current device state...")
    trigger_telemetry(mqtt_client)
    time.sleep(5)

    # ── Test 1: targetMode = 2 ───────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("TEST 1: notify-mode-changed  targetMode=2")
    print("  (From app traffic: this was sent when AI mode was activated)")
    print("=" * 65)
    result = notify_mode(target_mode=2)
    if result.get("code") == "0":
        print("  API accepted (code=0)")
    else:
        print(f"  API code: {result.get('code')}  msg: {result.get('message')}")

    print("\n  Watching telemetry for 20s...")
    trigger_telemetry(mqtt_client)
    time.sleep(20)

    # ── Test 2: targetMode = -1 ──────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("TEST 2: notify-mode-changed  targetMode=-1")
    print("  (From app traffic: this was sent when AI mode was deactivated)")
    print("=" * 65)
    result = notify_mode(target_mode=-1)
    if result.get("code") == "0":
        print("  API accepted (code=0)")

    print("\n  Watching telemetry for 20s...")
    trigger_telemetry(mqtt_client)
    time.sleep(20)

    # ── Test 3: targetMode = 0 ───────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("TEST 3: notify-mode-changed  targetMode=0")
    print("  (Guessing: Backup mode / charge from grid)")
    print("=" * 65)
    result = notify_mode(target_mode=0)
    print("\n  Watching telemetry for 20s...")
    trigger_telemetry(mqtt_client)
    time.sleep(20)

    # ── Test 4: targetMode = 1 ───────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("TEST 4: notify-mode-changed  targetMode=1")
    print("  (Guessing: Self-Powered mode / stop charging)")
    print("=" * 65)
    result = notify_mode(target_mode=1)
    print("\n  Watching telemetry for 20s...")
    trigger_telemetry(mqtt_client)
    time.sleep(20)

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("DONE. Telemetry changes observed:")
    with telemetry_lock:
        if telemetry_log:
            for ts, summary in telemetry_log:
                print(f"\n  [{ts}]")
                print(summary)
        else:
            print("  (no telemetry changes detected)")
    print("=" * 65)
    mqtt_client.loop_stop()

if __name__ == "__main__":
    main()
