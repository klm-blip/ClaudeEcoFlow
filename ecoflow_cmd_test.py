"""
EcoFlow Command Tester v7
=========================
Tests the CORRECT protobuf command structure for EcoFlow JTS1-platform devices
(Smart Home Panel 2, ESG, Smart Home Panel 3, PowerOcean).

KEY FIX vs v6: Completely wrong message structure in all prior versions.
The real structure (from ioBroker/foxthefox reverse engineering) is:

  outer {
    field 1: header {
      field  1: pdata      (nested protobuf: the actual command payload)
      field  2: src        = 2       (app source)
      field  3: dest       = 32      (EMS/device destination)
      field  8: cmd_func   = e.g. 96 (EMS command set)
      field  9: cmd_id     = e.g. 112 (specific command)
      field 11: need_ack   = 1
      field 14: seq        = Date.now() millis
      field 16: version    = 19
      field 17: payload_ver = 1
    }
  }

Prior versions used a completely different 2-level wrapping with SN/func/seq/pdata
in the wrong positions — the device ignored every message as a result.

JTS1 EMS commands (cmd_func=96):
  cmd_id=37:  EmsGetParam       - read current params (empty pdata, safe test)
  cmd_id=98:  SysWorkModeSet    - set operating mode
  cmd_id=112: SysBatChgDsgSet   - charge/discharge SoC limits
  cmd_id=97:  EnergyStreamSwitch- enable/disable energy stream
"""

import json
import os
import struct
import sys
import time
import traceback

# ── Log setup ─────────────────────────────────────────────────────────────────
_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cmd_test.log")
_log_f    = open(_log_path, "w", buffering=1, encoding="utf-8", errors="replace")

def log(msg=""):
    line = str(msg)
    try:
        sys.__stdout__.write(line + "\n"); sys.__stdout__.flush()
    except UnicodeEncodeError:
        sys.__stdout__.write(line.encode("ascii", "replace").decode() + "\n")
        sys.__stdout__.flush()
    _log_f.write(line + "\n"); _log_f.flush()

log("=== EcoFlow Command Tester v7 (Correct JTS1 setHeader structure) ===")
log(f"Log: {_log_path}")

# ── Imports ────────────────────────────────────────────────────────────────────
try:
    import paho.mqtt.client as mqtt
    try:
        _v = mqtt.CallbackAPIVersion.VERSION1
        USE_V1 = True
    except AttributeError:
        USE_V1 = False
except ImportError as e:
    log(f"FATAL: {e}  ->  pip install paho-mqtt")
    input("Press Enter..."); sys.exit(1)

# ── Credentials ────────────────────────────────────────────────────────────────
def _load_creds():
    _dir = os.path.dirname(os.path.abspath(__file__))
    c = {
        "MQTT_USER": "app-740f41d44de04eaf83832f8a801252e9",
        "MQTT_PASS": "c1e46f17f6994a1e8252f1e1f3135b68",
        "CLIENT_ID": "ANDROID_574080605_1971363830522871810",
    }
    f = os.path.join(_dir, "ecoflow_credentials.txt")
    if os.path.exists(f):
        for line in open(f).read().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() in c:
                    c[k.strip()] = v.strip()
    return c

creds     = _load_creds()
MQTT_USER = creds["MQTT_USER"]
MQTT_PASS = creds["MQTT_PASS"]
CLIENT_ID = creds["CLIENT_ID"]

_parts     = CLIENT_ID.split("_", 2)
SESSION_ID = _parts[2] if len(_parts) >= 3 else _parts[-1]

GATEWAY_SN = "HR65ZA1AVH7J0027"
MQTT_HOST  = "mqtt.ecoflow.com"
MQTT_PORT  = 8883

TELEMETRY_TOPICS = [
    f"/app/device/property/{GATEWAY_SN}",
    f"/app/device/property/P101ZA1A9HA70164",
]
CMD_TOPIC = f"/app/{SESSION_ID}/{GATEWAY_SN}/set"

log(f"CLIENT_ID:  {CLIENT_ID}")
log(f"SESSION_ID: {SESSION_ID}")
log(f"CMD_TOPIC:  {CMD_TOPIC}")

# ── Protobuf builder ───────────────────────────────────────────────────────────
def _vi(v):
    """Encode unsigned integer as protobuf varint."""
    out = []
    while True:
        out.append(v & 0x7F); v >>= 7
        if v == 0: break
    for i in range(len(out) - 1):
        out[i] |= 0x80
    return bytes(out)

def pb_int(field, v):
    """Wire type 0: varint field."""
    return _vi((field << 3) | 0) + _vi(v)

def pb_float(field, v):
    """Wire type 5: 32-bit float field."""
    return _vi((field << 3) | 5) + struct.pack('<f', v)

def pb_str(field, v):
    """Wire type 2: length-delimited string field."""
    b = v.encode()
    return _vi((field << 3) | 2) + _vi(len(b)) + b

def pb_msg(field, b):
    """Wire type 2: length-delimited nested message field."""
    return _vi((field << 3) | 2) + _vi(len(b)) + b


def build(cmd_func, cmd_id, pdata_bytes, seq=None,
          src=2, dest=32, need_ack=1, version=19, payload_ver=1):
    """
    Build a JTS1-platform command using the correct setHeader structure.

    outer {
      field 1: header {
        field  1: pdata       (nested command payload)
        field  2: src         = 2
        field  3: dest        = 32
        field  8: cmd_func
        field  9: cmd_id
        field 11: need_ack    = 1
        field 14: seq         (ms timestamp)
        field 16: version     = 19
        field 17: payload_ver = 1
      }
    }
    """
    if seq is None:
        seq = int(time.time() * 1000)  # millisecond timestamp like JS Date.now()
    header = (
        pb_msg(1,  pdata_bytes) +
        pb_int(2,  src)         +
        pb_int(3,  dest)        +
        pb_int(8,  cmd_func)    +
        pb_int(9,  cmd_id)      +
        pb_int(11, need_ack)    +
        pb_int(14, seq)         +
        pb_int(16, version)     +
        pb_int(17, payload_ver)
    )
    return pb_msg(1, header)


# ── Decoder ────────────────────────────────────────────────────────────────────
def _dvi(data, pos):
    r, s = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80): break
        s += 7
    return r, pos

def decode_all(data, prefix="", out=None):
    if out is None:
        out = {}
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _dvi(data, pos)
            fn = tag >> 3; wt = tag & 7
            if fn == 0: break
            key = f"{prefix}{fn}"
            if wt == 0:
                v, pos = _dvi(data, pos)
                out[key] = v
            elif wt == 2:
                ln, pos = _dvi(data, pos)
                raw = data[pos:pos+ln]; pos += ln
                decode_all(raw, f"{key}.", out)
            elif wt == 5:
                v = struct.unpack_from('<f', data, pos)[0]; pos += 4
                out[key] = round(v, 2)
            else:
                break
        except Exception:
            break
    return out

# ── Telemetry state ────────────────────────────────────────────────────────────
# batt=None means "no telemetry yet"; 0.0 is a real valid reading
state = {"batt": None, "load": None, "msgs": 0, "raw_fields": {}}

def on_message(client, userdata, msg):
    try:
        state["msgs"] += 1
        fields = decode_all(msg.payload)
        if len(state["raw_fields"]) == 0 and fields:
            state["raw_fields"] = dict(fields)
        for k, v in fields.items():
            sk = str(k)
            if sk.endswith("518") and isinstance(v, float) and -20000 < v < 20000:
                state["batt"] = round(v, 1)   # NOT "or None" — 0.0 is valid!
            if sk.endswith("1544") and isinstance(v, int) and 0 < v < 50000:
                state["load"] = v
    except Exception as e:
        log(f"  [msg err: {e}]")

# ── MQTT setup ─────────────────────────────────────────────────────────────────
if USE_V1:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
else:
    client = mqtt.Client(client_id=CLIENT_ID, protocol=mqtt.MQTTv311)

connected = [False]

def on_connect(c, u, f, rc):
    connected[0] = (rc == 0)
    log(f"on_connect rc={rc}  {'OK' if rc==0 else 'FAILED'}")
    if rc == 0:
        for t in TELEMETRY_TOPICS:
            c.subscribe(t, qos=1)
            log(f"  subscribed: {t}")

def on_disconnect(c, u, rc):
    log(f"on_disconnect rc={rc}")

client.username_pw_set(MQTT_USER, MQTT_PASS)
client.tls_set()
client.on_connect    = on_connect
client.on_message    = on_message
client.on_disconnect = on_disconnect

log(f"\nConnecting to {MQTT_HOST}:{MQTT_PORT}...")
client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
client.loop_start()

log("Waiting 4s for connection...")
time.sleep(4)
if not connected[0]:
    log("ERROR: Not connected after 4s"); input("Press Enter..."); sys.exit(1)

log("Collecting baseline telemetry for 15s...")
time.sleep(15)
log(f"Baseline: batt={state['batt']}W  load={state['load']}W  msgs={state['msgs']}")

if state["msgs"] == 0:
    log("WARNING: No telemetry received.")

log("\nFields from first telemetry message:")
if state["raw_fields"]:
    for k, v in sorted(state["raw_fields"].items(), key=lambda x: str(x[0])):
        log(f"  [{k}] = {v}")
else:
    log("  (none)")

# ── Test definitions ───────────────────────────────────────────────────────────
#
# All commands use the NEW correct structure (build() function above).
# cmd_func=96 = EMS_CMD_SETS (JTS1 EMS commands)
#
# cmd_id=37:  EmsGetParam        - empty pdata, safe read (causes device to reply)
# cmd_id=97:  EnergyStreamSwitch - ems_open_energy_stream field1=1 (enable)
# cmd_id=98:  SysWorkModeSet     - field1=work_mode (0=self-use,1=TOU,2=backup,4=AC_makeup)
# cmd_id=112: SysBatChgDsgSet    - field1=chg_up_limit%, field2=dsg_down_limit%
# cmd_id=115: SysFeedPowerSet    - field1=feed power watts?
#
# Also try cmd_func=12 (SHP2 ProtoPushAndSet) and cmd_func=20 (latestQuotas)
# with the NEW structure, in case ESG uses a different cmd_func than SHP2.

WATCH = 10   # seconds to watch after each command

tests = [
    # ---- EMS GET (empty pdata, should cause a response) ----
    ("EmsGetParam  (96/37 empty)",   96,  37, b""),

    # ---- Work mode: 4=AC_MAKEUP (force AC charge) ----
    ("SysWorkMode  (96/98 mode=4 AC_makeup)", 96, 98, pb_int(1, 4)),
    ("SysWorkMode  (96/98 mode=0 self-use)",  96, 98, pb_int(1, 0)),
    ("SysWorkMode  (96/98 mode=2 backup)",    96, 98, pb_int(1, 2)),

    # ---- SoC charge limit set (field1=chg_up_limit %) ----
    ("SysBatChgDsgSet (96/112 chg=100%)",     96, 112, pb_int(1, 100)),
    ("SysBatChgDsgSet (96/112 chg=95%)",      96, 112, pb_int(1, 95)),

    # ---- Energy stream enable ----
    ("EnergyStreamSwitch (96/97 enable=1)",   96,  97, pb_int(1, 1)),

    # ---- Try SHP2-era cmd_func=12 with NEW correct structure ----
    ("ProtoPushAndSet (12/32 chg_watt=1000)", 12,  32, pb_int(7, 1000)),
    ("ProtoPushAndSet (12/32 chg_watt=0)",    12,  32, pb_int(7, 0)),

    # ---- latestQuotas ping (from PowerOcean deviceCmd) ----
    ("latestQuotas   (20/1 empty)",           20,   1, b""),

    # ---- Version=4 variant of EmsGetParam (device telemetry uses v=4) ----
    ("EmsGetParam v=4 (96/37 empty)",         96,  37, b""),   # sent separately with version=4 below
]

log(f"\nRunning {len(tests)} tests x {WATCH}s each...")
log(f"CMD_TOPIC: {CMD_TOPIC}\n")

results = []

for i, (label, cmd_func, cmd_id, pdata) in enumerate(tests):
    log(f"\n{'-'*55}")
    log(f"TEST {i+1}/{len(tests)}: {label}")

    # Last test: send with version=4 to match device telemetry version
    use_version = 4 if "v=4" in label else 19

    payload = build(cmd_func, cmd_id, pdata, version=use_version)
    log(f"HEX ({len(payload)}b): {payload.hex()}")
    log(f"  cmd_func={cmd_func} cmd_id={cmd_id} version={use_version}")

    batt_before = state["batt"]
    msgs_before = state["msgs"]

    try:
        rc = client.publish(CMD_TOPIC, payload, qos=1)
        log(f"  published rc={rc.rc}")
    except Exception as e:
        log(f"  publish error: {e}")
        results.append((label, "error", batt_before, None, None))
        continue

    for tick in range(WATCH):
        time.sleep(1)
        cur   = state["batt"]
        delta = round(cur - batt_before, 1) if (cur is not None and batt_before is not None) else None
        new_msgs = state["msgs"] - msgs_before
        marker = ""
        if delta is not None:
            if abs(delta) > 80:   marker = "  <<< WORKED!"
            elif abs(delta) > 40: marker = "  << maybe"
        elif cur is not None and batt_before is None:
            marker = "  (baseline was None)"
        log(f"  [{tick+1:2d}s] batt={cur}W  load={state['load']}W  d={delta}W  new_msgs={new_msgs}{marker}")

    final_batt = state["batt"]
    delta = (round(final_batt - batt_before, 1)
             if (final_batt is not None and batt_before is not None) else None)
    status = ("WORKED" if delta is not None and abs(delta) > 80
              else "MAYBE"  if delta is not None and abs(delta) > 40
              else "no")
    results.append((label, status, batt_before, final_batt, delta))

    if status == "WORKED":
        log(f"\n*** WORKED: {label}  batt {batt_before}->{final_batt}W ***")
        break

# ── Summary ────────────────────────────────────────────────────────────────────
log(f"\n{'='*55}")
log("SUMMARY")
log(f"{'='*55}")
for label, status, bb, ba, delta in results:
    icon = ("*** WORKED" if status == "WORKED"
            else ("??  MAYBE " if status == "MAYBE" else "    ------"))
    log(f"{icon}  {label:<45}  {bb}->{ba}W  d={delta}W")

log(f"\nTotal telemetry messages: {state['msgs']}")
log(f"Log: {_log_path}")

client.loop_stop()
client.disconnect()
_log_f.flush()
input("\nPress Enter to close...")
