"""
EcoFlow REST Mode Change — Reconfirmation Test
================================================
Clean test to definitively verify whether notify-mode-changed
actually changes device behavior.

Steps:
  1. Connect MQTT, get baseline telemetry (mode, battery W, grid W, SOC)
  2. Send targetMode=2 (self-powered), observe telemetry changes
  3. Send targetMode=-1 (backup), observe telemetry changes
  4. Print clear before/after comparison

Run:  python ecoflow_reconfirm_test.py
"""

import json
import os
import struct
import sys
import time
import threading
import urllib.request
import ssl

# ── Config ────────────────────────────────────────────────────────────────────
GATEWAY_SN = "HR65ZA1AVH7J0027"
BASE_URL   = "https://api-a.ecoflow.com"
MQTT_HOST  = "mqtt.ecoflow.com"
MQTT_PORT  = 8883

# ── Load credentials ─────────────────────────────────────────────────────────
def load_credentials():
    cred_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "ecoflow_credentials.txt")
    creds = {}
    if not os.path.exists(cred_file):
        print(f"ERROR: {cred_file} not found")
        sys.exit(1)
    for line in open(cred_file).read().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip()
    return creds

creds    = load_credentials()
REST_JWT = creds.get("REST_JWT", "")
MQTT_USER = creds.get("MQTT_USER", "")
MQTT_PASS = creds.get("MQTT_PASS", "")
CLIENT_ID = creds.get("CLIENT_ID", "")

if not REST_JWT:
    print("ERROR: REST_JWT not found in ecoflow_credentials.txt")
    sys.exit(1)

parts = CLIENT_ID.split("_", 2)
SESSION_ID = parts[2] if len(parts) >= 3 else parts[-1]
rand_part = parts[1] if len(parts) >= 3 else "0"
TEST_CLIENT_ID = f"ANDROID_{int(rand_part)+5}_{SESSION_ID}"

print(f"JWT loaded: {REST_JWT[:20]}...")
print(f"MQTT user:  {MQTT_USER[:20]}...")
print(f"Client ID:  {TEST_CLIENT_ID}")

# ── Protobuf helpers ──────────────────────────────────────────────────────────
def decode_varint(data, pos):
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos

def parse_proto_fields(raw):
    out = {}
    pos = 0
    while pos < len(raw):
        try:
            tag, pos = decode_varint(raw, pos)
            fn = tag >> 3
            wt = tag & 0x07
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
            elif wt == 1:
                v = struct.unpack_from("<d", raw, pos)[0]; pos += 8
                out[fn] = ("double", v)
            else:
                break
        except Exception:
            break
    return out

# ── Telemetry state ───────────────────────────────────────────────────────────
class TelemetryState:
    def __init__(self):
        self.lock = threading.Lock()
        self.mode = None         # 1=backup, 2=self-powered
        self.soc = None
        self.battery_w = None
        self.grid_w = None
        self.home_w = None
        self.last_update = None
        self.snapshots = []      # list of (label, time, mode, soc, bat_w, grid_w, home_w)

    def update(self, fields):
        with self.lock:
            if 518 in fields and fields[518][0] == "float":
                self.battery_w = round(fields[518][1], 1)
            if 515 in fields:
                self.grid_w = round(fields[515][1], 1) if fields[515][0] == "float" else fields[515][1]
            if 1544 in fields:
                self.home_w = fields[1544][1]
            if 1009 in fields and fields[1009][0] == "bytes":
                sub = parse_proto_fields(fields[1009][1])
                if 4 in sub:
                    self.mode = sub[4][1]
                if 5 in sub:
                    self.soc = sub[5][1]
            self.last_update = time.strftime("%H:%M:%S")

    def snapshot(self, label):
        with self.lock:
            s = (label, self.last_update, self.mode, self.soc,
                 self.battery_w, self.grid_w, self.home_w)
            self.snapshots.append(s)
            return s

    def current_str(self):
        with self.lock:
            mode_str = {1: "BACKUP", 2: "SELF-POWERED"}.get(self.mode, str(self.mode))
            return (f"Mode={mode_str}  SOC={self.soc}%  "
                    f"Battery={self.battery_w}W  Grid={self.grid_w}W  Home={self.home_w}W")

state = TelemetryState()

# ── MQTT ──────────────────────────────────────────────────────────────────────
try:
    import paho.mqtt.client as mqtt
    HAS_MQTT = True
except ImportError:
    HAS_MQTT = False
    print("WARNING: paho-mqtt not installed — will test REST only (no telemetry)")

mqtt_connected = threading.Event()

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        topic = f"/app/device/property/{GATEWAY_SN}"
        client.subscribe(topic, qos=0)
        print(f"  MQTT connected, subscribed to {topic}")
        mqtt_connected.set()
    else:
        print(f"  MQTT connect failed: rc={rc}")

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
                fields = parse_proto_fields(pdata)
                state.update(fields)
    except Exception:
        pass

def start_mqtt():
    if not HAS_MQTT:
        return None
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                             client_id=TEST_CLIENT_ID, protocol=mqtt.MQTTv311)
    except (AttributeError, TypeError):
        client = mqtt.Client(client_id=TEST_CLIENT_ID, protocol=mqtt.MQTTv311)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client

def trigger_telemetry(client):
    if not client:
        return
    get_topic = f"/app/{SESSION_ID}/{GATEWAY_SN}/thing/property/get"
    trigger = json.dumps({
        "from": "HomeAssistant", "id": "999", "version": "1.1",
        "moduleType": 0, "operateType": "latestQuotas", "params": {}
    })
    client.publish(get_topic, trigger, qos=1)

# ── REST ──────────────────────────────────────────────────────────────────────
def rest_notify_mode(target_mode):
    url = f"{BASE_URL}/tou-service/goe/ai-mode/notify-mode-changed"
    hdrs = {
        "Authorization": f"Bearer {REST_JWT}",
        "Content-Type": "application/json",
        "lang": "en-us",
        "countryCode": "US",
        "platform": "android",
        "version": "6.11.0.1731",
        "User-Agent": "okhttp/4.11.0",
        "X-Appid": "-1",
    }
    body = json.dumps({"sn": GATEWAY_SN, "systemNo": "", "targetMode": target_mode}).encode()
    try:
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
        code = result.get("code")
        msg = result.get("message", "?")
        return code, msg, result
    except Exception as e:
        return None, str(e), {}

# ── Main test ─────────────────────────────────────────────────────────────────
def main():
    print()
    print("=" * 65)
    print("  EcoFlow REST Mode Change — Reconfirmation Test")
    print("=" * 65)

    # 1. Connect MQTT
    print("\n[1] Connecting to MQTT for telemetry monitoring...")
    mqtt_client = start_mqtt()
    if mqtt_client:
        if not mqtt_connected.wait(timeout=10):
            print("  WARNING: MQTT connection timed out")
    else:
        print("  (skipping MQTT — paho not available)")

    # 2. Get baseline
    print("\n[2] Getting baseline telemetry...")
    trigger_telemetry(mqtt_client)
    time.sleep(5)
    trigger_telemetry(mqtt_client)
    time.sleep(5)

    baseline = state.snapshot("BASELINE")
    print(f"  BASELINE: {state.current_str()}")

    if state.mode is None and mqtt_client:
        print("  WARNING: No telemetry received. MQTT may not be working.")
        print("  Continuing with REST test anyway...")

    # 3. Test targetMode=2 (self-powered)
    print("\n" + "─" * 65)
    print("[3] Sending targetMode=2 (self-powered / stop charging)...")
    code, msg, result = rest_notify_mode(2)
    print(f"  HTTP response: code={code}  message={msg}")
    if code != "0":
        print(f"  >>> REST CALL FAILED — this endpoint may not be working")
        print(f"  Full response: {json.dumps(result, indent=2)}")
    else:
        print(f"  >>> REST accepted (code=0)")

    print("  Waiting 15s for device to react...")
    for i in range(3):
        time.sleep(5)
        trigger_telemetry(mqtt_client)

    after_mode2 = state.snapshot("AFTER targetMode=2")
    print(f"  AFTER MODE 2: {state.current_str()}")

    # 4. Test targetMode=-1 (backup / allow charging)
    print("\n" + "─" * 65)
    print("[4] Sending targetMode=-1 (backup / allow charging)...")
    code, msg, result = rest_notify_mode(-1)
    print(f"  HTTP response: code={code}  message={msg}")
    if code != "0":
        print(f"  >>> REST CALL FAILED")
        print(f"  Full response: {json.dumps(result, indent=2)}")
    else:
        print(f"  >>> REST accepted (code=0)")

    print("  Waiting 15s for device to react...")
    for i in range(3):
        time.sleep(5)
        trigger_telemetry(mqtt_client)

    after_mode_neg1 = state.snapshot("AFTER targetMode=-1")
    print(f"  AFTER MODE -1: {state.current_str()}")

    # 5. Summary
    print("\n" + "=" * 65)
    print("  RESULTS SUMMARY")
    print("=" * 65)
    print(f"{'Label':<22} {'Mode':<15} {'SOC':<6} {'Battery W':<12} {'Grid W':<10} {'Home W':<10}")
    print("─" * 75)
    for label, ts, mode, soc, bat, grid, home in state.snapshots:
        mode_str = {1: "BACKUP", 2: "SELF-POWERED"}.get(mode, str(mode))
        print(f"{label:<22} {mode_str:<15} {str(soc)+'%':<6} {str(bat):<12} {str(grid):<10} {str(home):<10}")

    print()
    # Verdict
    modes = [s[2] for s in state.snapshots]
    if len(set(modes)) > 1 and None not in modes:
        print(">>> VERDICT: Mode DID change between commands — REST endpoint has physical effect!")
    elif all(m is None for m in modes):
        print(">>> VERDICT: No telemetry received — cannot confirm physical effect.")
        print("    (REST calls may have succeeded but we can't verify without telemetry)")
    else:
        print(">>> VERDICT: Mode did NOT change — REST endpoint may not have physical effect.")
        print("    Or: the mode was already in the target state before the command.")

    print()
    if mqtt_client:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()

    print("=" * 65)
    input("Press Enter to close...")

if __name__ == "__main__":
    main()
