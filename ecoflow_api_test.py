"""
EcoFlow Developer REST API Tester v2
=====================================
Correct signature algorithm:
  sorted_query_params + "&accessKey=X&nonce=Y&timestamp=Z"
  (accessKey/nonce/timestamp appended LAST, not sorted with the rest)

Steps:
  1. Get device list (verify auth)
  2. Get all quotas for gateway (live state)
  3. Get developer MQTT credentials via /certification
  4. Connect to developer MQTT, subscribe to telemetry, send a test command

Pass --set to also send a charge command over developer MQTT.
"""

import hashlib
import hmac
import json
import os
import random
import string
import sys
import time
import urllib.parse
import urllib.request

# ── Load credentials ───────────────────────────────────────────────────────────
def load_creds():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecoflow_credentials.txt")
    c = {}
    for line in open(path).read().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            c[k.strip()] = v.strip()
    return c

creds      = load_creds()
ACCESS_KEY = creds["ACCESS_KEY"]
SECRET_KEY = creds["SECRET_KEY"]
GATEWAY_SN = "HR65ZA1AVH7J0027"
BASE_URL   = "https://api.ecoflow.com"

print(f"ACCESS_KEY: {ACCESS_KEY[:8]}...")
print(f"GATEWAY_SN: {GATEWAY_SN}")

# ── Signature (CORRECTED) ──────────────────────────────────────────────────────
def _sign(query_params: dict, access_key: str, nonce: str, timestamp: str, secret: str) -> str:
    """
    EcoFlow signature algorithm (from public_api.py):
      1. Sort actual query params alphabetically, join as k=v&k=v
      2. Append &accessKey=X&nonce=Y&timestamp=Z
      3. HMAC-SHA256 the result with secret_key
    """
    if query_params:
        sorted_str = "&".join(f"{k}={v}" for k, v in sorted(query_params.items()))
        target = f"{sorted_str}&accessKey={access_key}&nonce={nonce}&timestamp={timestamp}"
    else:
        target = f"accessKey={access_key}&nonce={nonce}&timestamp={timestamp}"
    print(f"  [sign] target: {target[:80]}...")
    return hmac.new(secret.encode("utf-8"), target.encode("utf-8"), hashlib.sha256).hexdigest()

def _auth_headers(query_params: dict, include_content_type: bool = False) -> dict:
    nonce = str(random.randint(10000, 1000000))
    ts    = str(int(time.time() * 1000))
    sign  = _sign(query_params, ACCESS_KEY, nonce, ts, SECRET_KEY)
    h = {
        "accessKey": ACCESS_KEY,
        "nonce":     nonce,
        "timestamp": ts,
        "sign":      sign,
    }
    if include_content_type:
        h["Content-Type"] = "application/json;charset=UTF-8"
    return h

# ── HTTP helpers ───────────────────────────────────────────────────────────────
def get(path, params=None):
    if params is None:
        params = {}
    headers = _auth_headers(params)
    qs  = urllib.parse.urlencode(params)
    url = f"{BASE_URL}{path}?{qs}" if qs else f"{BASE_URL}{path}"
    print(f"\nGET {url}")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()}
    except Exception as e:
        return {"error": str(e)}

def post(path, body: dict):
    headers = _auth_headers(body, include_content_type=True)
    url  = f"{BASE_URL}{path}"
    data = json.dumps(body).encode()
    print(f"\nPOST {url}  body={json.dumps(body)}")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()}
    except Exception as e:
        return {"error": str(e)}

# ── 1. Device list ─────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("1. GET /iot-open/sign/device/list")
resp = get("/iot-open/sign/device/list")
print(json.dumps(resp, indent=2))

# ── 2. All quotas ──────────────────────────────────────────────────────────────
print("\n" + "="*60)
print(f"2. GET /iot-open/sign/device/quota/all?sn={GATEWAY_SN}")
resp = get("/iot-open/sign/device/quota/all", {"sn": GATEWAY_SN})
if resp.get("code") == "0" and "data" in resp:
    print(f"  code=0 OK  keys={len(resp['data'])}")
    # Print a useful subset
    interesting = [
        "wattInfo.gridWatt", "wattInfo.allHallWatt",
        "backupIncreInfo.backupBatPer",
        "chargeWattPower", "foceChargeHight", "backupReserveSoc",
        "smartBackupMode", "epsModeInfo", "stormIsEnable",
        "pd303_mc.masterIncreInfo.gridSta",
    ]
    print("  --- Key values ---")
    for k in interesting:
        if k in resp["data"]:
            print(f"  {k} = {resp['data'][k]}")
    print("\n  --- All keys ---")
    for k, v in sorted(resp["data"].items()):
        print(f"  {k} = {v}")
else:
    print(json.dumps(resp, indent=2))

# ── 3. Developer MQTT certification ───────────────────────────────────────────
print("\n" + "="*60)
print("3. GET /iot-open/sign/certification  (developer MQTT credentials)")
cert = get("/iot-open/sign/certification")
print(json.dumps(cert, indent=2))

# ── 4. Developer MQTT test ────────────────────────────────────────────────────
if cert.get("code") == "0" and "data" in cert:
    d = cert["data"]
    mqtt_host = d.get("url", "mqtt.ecoflow.com")
    mqtt_port = int(d.get("port", 8883))
    mqtt_user = d.get("certificateAccount", "")
    mqtt_pass = d.get("certificatePassword", "")

    print(f"\nDeveloper MQTT broker: {mqtt_host}:{mqtt_port}")
    print(f"  certificateAccount: {mqtt_user}")

    try:
        import paho.mqtt.client as mqtt

        # Topics for developer MQTT
        telem_topic = f"/open/{mqtt_user}/{GATEWAY_SN}/quota"
        set_topic   = f"/open/{mqtt_user}/{GATEWAY_SN}/set"
        reply_topic = f"/open/{mqtt_user}/{GATEWAY_SN}/set_reply"
        status_topic= f"/open/{mqtt_user}/{GATEWAY_SN}/status"
        print(f"  telem_topic: {telem_topic}")
        print(f"  set_topic:   {set_topic}")

        msgs = []
        def on_connect(c, u, f, rc):
            print(f"  on_connect rc={rc}")
            if rc == 0:
                for t in [telem_topic, reply_topic, status_topic]:
                    c.subscribe(t, qos=1)
                    print(f"  subscribed: {t}")

        def on_message(c, u, msg):
            try:
                payload = json.loads(msg.payload.decode())
                msgs.append((msg.topic, payload))
                print(f"  MSG on {msg.topic}:")
                print(f"  {json.dumps(payload, indent=4)}")
            except Exception:
                print(f"  MSG on {msg.topic} (raw {len(msg.payload)}b): {msg.payload[:200]}")

        def on_subscribe(c, u, mid, granted):
            print(f"  SUBACK mid={mid} granted={granted}")

        try:
            _v = mqtt.CallbackAPIVersion.VERSION1
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                 client_id=f"HOMEAUTO_{ACCESS_KEY[:8]}",
                                 protocol=mqtt.MQTTv311)
        except AttributeError:
            client = mqtt.Client(client_id=f"HOMEAUTO_{ACCESS_KEY[:8]}", protocol=mqtt.MQTTv311)

        client.username_pw_set(mqtt_user, mqtt_pass)
        client.tls_set()
        client.on_connect   = on_connect
        client.on_message   = on_message
        client.on_subscribe = on_subscribe

        print(f"\nConnecting to developer MQTT...")
        client.connect(mqtt_host, mqtt_port, keepalive=60)
        client.loop_start()

        print("Waiting 60s for telemetry (developer MQTT may push every 30-60s)...")
        time.sleep(60)

        print(f"\n  Received {len(msgs)} messages so far")

        if "--set" in sys.argv:
            # Send AC charge power command via developer MQTT
            seq = str(random.randint(100000000, 900000000))
            cmd = {
                "from":    "HomeAssistant",
                "id":      seq,
                "version": "1.0",
                "sn":      GATEWAY_SN,
                "cmdCode": "PD303_APP_SET",
                "params":  {"chargeWattPower": 1000},
            }
            print(f"\n--- SENDING chargeWattPower=1000 (AC charge ON) ---")
            print(f"  topic: {set_topic}")
            print(f"  payload: {json.dumps(cmd)}")
            rc = client.publish(set_topic, json.dumps(cmd), qos=1)
            print(f"  publish rc={rc.rc}")
            print("  Waiting 20s for set_reply and telemetry change...")
            time.sleep(20)

        print(f"\nTotal messages received: {len(msgs)}")
        client.loop_stop()
        client.disconnect()

    except ImportError:
        print("paho-mqtt not installed — skipping MQTT test")
else:
    print("Certification failed — skipping MQTT test")

print("\nDone.")
