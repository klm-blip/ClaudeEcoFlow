"""
EcoFlow MQTT Sniffer v3
=======================
Subscribe to ALL topics for your gateway and log every message.

KEY FIX (v2→v3): Uses a VALID alternate CLIENT_ID instead of the broken
_SNIFF suffix (which was malformed and caused rc=5 broker rejection).

Format: ANDROID_{different_random}_{USER_ID}
The random segment is changed so the sniffer and phone app can coexist.
The USER_ID (3rd segment) is kept identical for correct topic routing.

WHAT THIS CAPTURES:
  - All device telemetry on /app/device/property/{SN}
  - Any commands or replies on /app/{USER_ID}/#

NOTE: EcoFlow phone app uses cloud REST (not direct MQTT publish).
Cloud-to-device commands arrive on the EcoFlow cloud's own MQTT session
(not our session), so app commands won't appear here directly.
However, device STATE CHANGES (mode, SOC, watts) DO appear in telemetry.

To capture the actual command format: use mitmproxy to intercept HTTPS.

Run WHILE using the EcoFlow app, watch for telemetry changes.
Log is written to sniffer_captures.log.

Run:
  python ecoflow_sniffer.py
"""

import json
import os
import struct
import time
import paho.mqtt.client as mqtt

# ── Load credentials ───────────────────────────────────────────────────────────
def _load_credentials():
    _dir      = os.path.dirname(os.path.abspath(__file__))
    cred_file = os.path.join(_dir, "ecoflow_credentials.txt")
    creds = {
        "MQTT_USER": "app-740f41d44de04eaf83832f8a801252e9",
        "MQTT_PASS": "c1e46f17f6994a1e8252f1e1f3135b68",
        "CLIENT_ID": "ANDROID_574080605_1971363830522871810",
    }
    if os.path.exists(cred_file):
        for line in open(cred_file).read().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k in creds:
                    creds[k] = v
        print(f"Credentials loaded from {cred_file}")
    return creds

creds         = _load_credentials()
MQTT_USER     = creds["MQTT_USER"]
MQTT_PASS     = creds["MQTT_PASS"]
BASE_CLIENT_ID = creds["CLIENT_ID"]

# SESSION_ID = 3rd segment of CLIENT_ID (e.g. "1971363830522871810")
# This is the routing ID used in MQTT topics: /app/{SESSION_ID}/{device}/set
_parts     = BASE_CLIENT_ID.split("_", 2)
_rand_seg  = _parts[1] if len(_parts) >= 3 else "574080605"
SESSION_ID = _parts[2] if len(_parts) >= 3 else _parts[-1]

# Generate a valid alternate CLIENT_ID: ANDROID_{different_rand}_{USER_ID}
# Increment the random segment by 1 — keeps valid format, avoids collision
_new_rand = str(int(_rand_seg) + 1) if _rand_seg.isdigit() else "574080606"
SNIFFER_CLIENT_ID = f"ANDROID_{_new_rand}_{SESSION_ID}"

GATEWAY_SN  = "HR65ZA1AVH7J0027"
INVERTER_SN = "P101ZA1A9HA70164"
MQTT_HOST   = "mqtt.ecoflow.com"
MQTT_PORT   = 8883

# Subscribe to every topic format EcoFlow might use
TOPICS = [
    f"/app/{SESSION_ID}/#",                    # ALL session messages (commands, replies, acks)
    f"/app/device/property/{GATEWAY_SN}",       # Gateway telemetry
    f"/app/device/property/{INVERTER_SN}",      # Inverter telemetry
    f"/{GATEWAY_SN}/#",                         # Alternative gateway format
    f"/{INVERTER_SN}/#",                        # Alternative inverter format
]

_dir     = os.path.dirname(os.path.abspath(__file__))
log_file = os.path.join(_dir, "sniffer_captures.log")

# Clear old log at startup
open(log_file, "w").close()


# ── Protobuf decoder (full recursive with string detection) ───────────────────
def decode_varint(data, pos):
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def decode_proto(data, depth=0):
    """Full recursive protobuf decode. Returns list of (field_num, wire_type, value)."""
    fields = []
    pos = 0
    while pos < len(data):
        try:
            tag, pos = decode_varint(data, pos)
            field_num = tag >> 3
            wire_type = tag & 0x07
            if field_num == 0:
                break
            if wire_type == 0:
                val, pos = decode_varint(data, pos)
                fields.append((field_num, "int", val))
            elif wire_type == 2:
                length, pos = decode_varint(data, pos)
                raw = data[pos: pos + length]; pos += length
                # Try UTF-8 string first (catches device serial numbers, etc.)
                try:
                    s = raw.decode("utf-8")
                    if s.isprintable() and len(s) > 0:
                        fields.append((field_num, "str", s))
                        continue
                except Exception:
                    pass
                # Try nested protobuf
                if depth < 6 and len(raw) >= 2:
                    try:
                        nested = decode_proto(raw, depth + 1)
                        if nested:
                            fields.append((field_num, "msg", nested))
                            continue
                    except Exception:
                        pass
                fields.append((field_num, "bytes", raw.hex()))
            elif wire_type == 5:
                val = struct.unpack_from("<f", data, pos)[0]; pos += 4
                fields.append((field_num, "float", round(val, 4)))
            else:
                fields.append((field_num, "?", f"[wire_type={wire_type}]"))
                break
        except Exception as e:
            fields.append(("_err", "err", str(e)))
            break
    return fields


def format_proto(fields, indent=0):
    """Format decoded protobuf fields as human-readable text."""
    lines = []
    pad = "  " * indent
    for field_num, wt, val in fields:
        if wt == "msg":
            lines.append(f"{pad}[{field_num}] msg {{")
            lines.extend(format_proto(val, indent + 1))
            lines.append(f"{pad}}}")
        else:
            lines.append(f"{pad}[{field_num}] {wt} = {val!r}")
    return lines


# ── Message counts ─────────────────────────────────────────────────────────────
msg_count     = 0
command_count = 0


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"\n Connected to {MQTT_HOST} as {SNIFFER_CLIENT_ID}")
        print(f"  SESSION_ID for topics: {SESSION_ID}")
        print(f"  Subscribing to {len(TOPICS)} topics...\n")
        for t in TOPICS:
            client.subscribe(t, qos=1)
            print(f"  SUB: {t}")
        print()
        print("=" * 70)
        print("SNIFFER READY -- NOW USE THE ECOFLOW APP TO:")
        print("  1. Switch mode: Backup -> Self-Powered")
        print("  2. Switch back: Self-Powered -> Backup")
        print("  3. Start AC charging (set a specific watt rate)")
        print("  4. Stop AC charging")
        print()
        print("Telemetry = dots  |  Commands/replies = full decode")
        print(f"Log: {log_file}")
        print("=" * 70)
        print()
    else:
        print(f"  Connect failed rc={rc}")


def on_message(client, userdata, msg):
    global msg_count, command_count
    msg_count += 1
    ts      = time.strftime("%H:%M:%S")
    topic   = msg.topic
    payload = msg.payload

    is_telemetry = "device/property" in topic and len(payload) > 200

    if is_telemetry:
        print(".", end="", flush=True)
    else:
        command_count += 1
        sep = "=" * 70
        print(f"\n{sep}")
        print(f"[{ts}] CMD #{command_count}  TOPIC: {topic}  ({len(payload)} bytes)")
        print(f"  HEX: {payload.hex()}")
        print()

        # Try JSON first
        try:
            parsed = json.loads(payload)
            print(f"  JSON:\n{json.dumps(parsed, indent=4)}")
        except Exception:
            # Try full protobuf decode
            try:
                decoded = decode_proto(payload)
                if decoded:
                    print("  PROTOBUF:")
                    for line in format_proto(decoded, indent=2):
                        print(line)
                else:
                    print("  (empty decode)")
            except Exception as e:
                print(f"  Decode error: {e}")
                print(f"  Raw: {payload!r}")

    # Log everything to file
    with open(log_file, "a") as f:
        f.write(f"\n[{ts}] TOPIC: {topic}  ({len(payload)} bytes)\n")
        f.write(f"  HEX: {payload.hex()}\n")
        if not is_telemetry:
            try:
                parsed = json.loads(payload)
                f.write(f"  JSON: {json.dumps(parsed, indent=2)}\n")
            except Exception:
                try:
                    decoded = decode_proto(payload)
                    f.write("  PROTOBUF:\n")
                    for line in format_proto(decoded, indent=2):
                        f.write(line + "\n")
                except Exception:
                    f.write(f"  Raw: {payload[:200]!r}\n")


def on_disconnect(client, userdata, rc):
    print(f"\n[disconnected rc={rc}]")


# ── Build client ───────────────────────────────────────────────────────────────
try:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION1,
        client_id=SNIFFER_CLIENT_ID,
        protocol=mqtt.MQTTv311,
    )
except AttributeError:
    client = mqtt.Client(client_id=SNIFFER_CLIENT_ID, protocol=mqtt.MQTTv311)

client.username_pw_set(MQTT_USER, MQTT_PASS)
client.tls_set()
client.on_connect    = on_connect
client.on_message    = on_message
client.on_disconnect = on_disconnect

print(f"\nEcoFlow MQTT Sniffer v2")
print(f"Gateway:    {GATEWAY_SN}")
print(f"Session ID: {SESSION_ID}")
print(f"Client ID:  {SNIFFER_CLIENT_ID}")
print(f"Log file:   {log_file}")
print(f"\nConnecting to {MQTT_HOST}:{MQTT_PORT}...")

client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

try:
    client.loop_forever()
except KeyboardInterrupt:
    print(f"\n\nStopped. Captured {msg_count} total ({command_count} non-telemetry).")
    print(f"Log: {log_file}")
