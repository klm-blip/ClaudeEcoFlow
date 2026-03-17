#!/usr/bin/env python3
"""
EcoFlow Developer API → MQTT Credential Acquisition + Command Test

Uses the official EcoFlow IoT Open Platform developer API to:
1. Get MQTT credentials (certificateAccount, certificatePassword, broker URL)
2. Connect to MQTT using those credentials
3. Test a known-working command (mode switch) to verify protobuf works via developer API

This is the DURABLE auth path — developer ACCESS_KEY/SECRET_KEY don't expire,
unlike the app-captured MQTT credentials which may be session-based.

Developer API signing:
  1. Sort all params by ASCII key, join with = and &
  2. Append accessKey={AK}&nonce={N}&timestamp={T}
  3. sign = HMAC-SHA256(secretKey, concatenated_string).hexdigest()
  4. Send headers: accessKey, nonce, timestamp, sign
"""

import sys
import ssl
import time
import json
import random
import hashlib
import hmac
import urllib.request
import urllib.error
import paho.mqtt.client as mqtt

# ──────────── Config ────────────
# Load from credentials file
CREDS_FILE = "ecoflow_credentials.txt"
ACCESS_KEY = ""
SECRET_KEY = ""
SN_HR65 = "HR65ZA1AVH7J0027"
SN_DPUX = "P101ZA1A9HA70164"
SN = SN_HR65

API_HOST = "https://api.ecoflow.com"
CERT_ENDPOINT = "/iot-open/sign/certification"
DEVICE_LIST_ENDPOINT = "/iot-open/sign/device/list"
QUOTA_ALL_ENDPOINT = "/iot-open/sign/device/quota/all"

def load_credentials():
    global ACCESS_KEY, SECRET_KEY
    try:
        with open(CREDS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ACCESS_KEY="):
                    ACCESS_KEY = line.split("=", 1)[1]
                elif line.startswith("SECRET_KEY="):
                    SECRET_KEY = line.split("=", 1)[1]
    except FileNotFoundError:
        print(f"[ERROR] {CREDS_FILE} not found")
        sys.exit(1)

    if not ACCESS_KEY or not SECRET_KEY:
        print("[ERROR] ACCESS_KEY or SECRET_KEY not found in credentials file")
        sys.exit(1)
    print(f"[OK] Loaded developer API credentials (accessKey: {ACCESS_KEY[:8]}...)")


# ──────────── EcoFlow Developer API Signing ────────────

def ecoflow_sign(params=None):
    """
    Build signed headers for EcoFlow Developer API.

    1. Sort params by key (ASCII), join with = and &
    2. Append accessKey, nonce, timestamp
    3. HMAC-SHA256 sign with SECRET_KEY
    """
    nonce = str(random.randint(100000, 999999))
    timestamp = str(int(time.time() * 1000))

    # Start with any query/body params
    all_params = dict(params) if params else {}

    # Build the sign string: sorted params + auth fields
    # All params including accessKey, nonce, timestamp must be sorted together
    all_params["accessKey"] = ACCESS_KEY
    all_params["nonce"] = nonce
    all_params["timestamp"] = timestamp

    # Sort by key and join
    sorted_keys = sorted(all_params.keys())
    sign_str = "&".join(f"{k}={all_params[k]}" for k in sorted_keys)

    # HMAC-SHA256
    sign = hmac.new(
        SECRET_KEY.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "accessKey": ACCESS_KEY,
        "nonce": nonce,
        "timestamp": timestamp,
        "sign": sign,
        "Content-Type": "application/json",
    }
    return headers


def api_get(endpoint, params=None):
    """Make a signed GET request to the EcoFlow Developer API."""
    headers = ecoflow_sign(params)
    url = API_HOST + endpoint
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url += "?" + query

    print(f"\n[API] GET {url}")
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"[API] Response code: {data.get('code', 'N/A')}")
            return data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        print(f"[API] HTTP {e.code}: {body[:500]}")
        return None
    except Exception as e:
        print(f"[API] Error: {e}")
        return None


# ──────────── Protobuf encoding (copied from test script) ────────────

def encode_varint(value):
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)

def encode_field_varint(field_number, value, force=False):
    if value == 0 and not force:
        return b""
    tag = (field_number << 3) | 0
    return encode_varint(tag) + encode_varint(value)

def encode_field_bool(field_number, value):
    if not value:
        return b""
    tag = (field_number << 3) | 0
    return encode_varint(tag) + encode_varint(1)

def encode_field_bytes(field_number, data):
    tag = (field_number << 3) | 2
    return encode_varint(tag) + encode_varint(len(data)) + data

def encode_field_string(field_number, s):
    return encode_field_bytes(field_number, s.encode("utf-8"))

def encode_field_message(field_number, message_bytes):
    return encode_field_bytes(field_number, message_bytes)


def build_mode_command(self_powered=False):
    """Build a mode switch command (backup or self-powered)."""
    mode_msg = b""
    mode_msg += encode_field_bool(1, self_powered)
    # fields 2-6 are false (omitted)
    config_write = encode_field_message(544, mode_msg)
    return config_write

def build_header(pdata, seq):
    msg = b""
    msg += encode_field_bytes(1, pdata)
    msg += encode_field_varint(2, 32)       # src
    msg += encode_field_varint(3, 11)       # dest (SHP3)
    msg += encode_field_varint(4, 1)        # dSrc
    msg += encode_field_varint(5, 1)        # dDest
    msg += encode_field_varint(8, 254)      # cmdFunc
    msg += encode_field_varint(9, 17)       # cmdId
    msg += encode_field_varint(10, len(pdata))
    msg += encode_field_varint(11, 1)       # needAck
    msg += encode_field_varint(14, seq)
    msg += encode_field_varint(15, 1)       # productId
    msg += encode_field_varint(16, 19)      # version
    msg += encode_field_varint(17, 1)       # payloadVer
    msg += encode_field_string(23, "Android")
    return msg

def build_send_header_msg(header_bytes):
    return encode_field_message(1, header_bytes)


# ──────────── Main ────────────

def main():
    load_credentials()

    print("\n" + "=" * 60)
    print("Step 1: Get MQTT credentials from Developer API")
    print("=" * 60)

    cert_data = api_get(CERT_ENDPOINT)
    if not cert_data or cert_data.get("code") != "0":
        print(f"\n[ERROR] Certification failed: {cert_data}")
        print("\nPossible issues:")
        print("  - ACCESS_KEY/SECRET_KEY may be invalid or not yet approved")
        print("  - API host might be different (try api-e.ecoflow.com for EU)")
        sys.exit(1)

    cert_info = cert_data.get("data", {})
    mqtt_user = cert_info.get("certificateAccount", "")
    mqtt_pass = cert_info.get("certificatePassword", "")
    mqtt_host = cert_info.get("url", "")
    mqtt_port = int(cert_info.get("port", 8883))
    mqtt_protocol = cert_info.get("protocol", "mqtts")

    print(f"\n[OK] Got MQTT credentials:")
    print(f"  Account:  {mqtt_user}")
    print(f"  Password: {mqtt_pass[:8]}...")
    print(f"  Broker:   {mqtt_host}:{mqtt_port} ({mqtt_protocol})")

    # Also try to list devices
    print("\n" + "=" * 60)
    print("Step 2: List registered devices")
    print("=" * 60)

    devices_data = api_get(DEVICE_LIST_ENDPOINT)
    if devices_data and devices_data.get("code") == "0":
        devices = devices_data.get("data", [])
        print(f"\n[OK] Found {len(devices)} device(s):")
        for d in devices:
            print(f"  - SN: {d.get('sn', '?')}  Online: {d.get('online', '?')}  Product: {d.get('productName', '?')}")
    else:
        print(f"[WARN] Device list failed: {devices_data}")

    # Developer API MQTT uses different topic format
    # /open/{certificateAccount}/{sn}/set       (commands)
    # /open/{certificateAccount}/{sn}/set_reply  (replies)
    # /open/{certificateAccount}/{sn}/quota      (telemetry)
    set_topic = f"/open/{mqtt_user}/{SN}/set"
    set_reply_topic = f"/open/{mqtt_user}/{SN}/set_reply"
    quota_topic = f"/open/{mqtt_user}/{SN}/quota"

    print(f"\n[INFO] Developer API MQTT topics:")
    print(f"  SET:       {set_topic}")
    print(f"  SET_REPLY: {set_reply_topic}")
    print(f"  QUOTA:     {quota_topic}")

    # Check command-line arg
    if len(sys.argv) > 1 and sys.argv[1] == "--test-command":
        print("\n" + "=" * 60)
        print("Step 3: Test MQTT command (backup mode) via Developer API")
        print("=" * 60)

        received = []

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                print(f"[MQTT] Connected to {mqtt_host}")
                client.subscribe(set_reply_topic, qos=1)
                client.subscribe(quota_topic, qos=1)
                print(f"[MQTT] Subscribed to {set_reply_topic}")
                print(f"[MQTT] Subscribed to {quota_topic}")
            else:
                print(f"[MQTT] Connection failed, rc={rc}")

        def on_message(client, userdata, msg):
            ts = time.strftime("%H:%M:%S")
            if "set_reply" in msg.topic:
                print(f"\n[{ts}] SET REPLY ({len(msg.payload)} bytes): {msg.payload.hex()[:80]}")
                received.append(msg)
            elif "quota" in msg.topic:
                print(f"[{ts}] QUOTA ({len(msg.payload)} bytes)")

        # Use certificateAccount as client ID prefix
        client_id = f"{mqtt_user}-test-{random.randint(1000,9999)}"
        client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
        client.username_pw_set(mqtt_user, mqtt_pass)
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
        client.on_connect = on_connect
        client.on_message = on_message

        print(f"\n[MQTT] Connecting to {mqtt_host}:{mqtt_port}...")
        client.connect(mqtt_host, mqtt_port, keepalive=60)
        client.loop_start()
        time.sleep(3)

        if not client.is_connected():
            print("[ERROR] Failed to connect to developer API MQTT broker")
            client.loop_stop()
            sys.exit(1)

        # Build backup mode command (same protobuf, different topic)
        config_write = build_mode_command(self_powered=False)  # backup = all false
        seq = random.randint(100000, 999999)
        header = build_header(config_write, seq)
        send_msg = build_send_header_msg(header)

        print(f"\n[MQTT] Sending backup mode command ({len(send_msg)} bytes) to {set_topic}")
        result = client.publish(set_topic, send_msg, qos=1)
        print(f"[MQTT] Publish result: rc={result.rc}")

        print("\nWaiting 15 seconds for reply...")
        for i in range(15):
            time.sleep(1)
            if i % 5 == 4:
                print(f"  ... {i+1}s, {len(received)} replies")

        print(f"\n{'='*60}")
        print(f"RESULTS: {len(received)} replies received")
        if received:
            print("[OK] Developer API MQTT is working with protobuf commands!")
        else:
            print("[WARN] No reply — developer API may use different topic or message format")
            print("  Try: the /open/ topics might need JSON wrapping instead of raw protobuf")
        print("=" * 60)

        client.loop_stop()
        client.disconnect()
    else:
        print("\n[INFO] Run with --test-command to also test sending a protobuf command")

    print("\nDone.")


if __name__ == "__main__":
    main()
