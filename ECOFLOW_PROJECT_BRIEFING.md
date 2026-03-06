# EcoFlow Energy Dashboard — Project Briefing for Claude Code

## The Goal

Build a home energy automation system that **automatically controls an EcoFlow battery system based on real-time electricity prices**, saving money by:
- Charging the battery when ComEd prices are low or negative (grid pays you)
- Discharging / running the home from battery when prices are high
- Switching between Backup and Self-Powered modes automatically based on price thresholds
- Handling negative pricing periods (ComEd BESH plan prices occasionally go below zero)

The user is on ComEd's **Hourly Pricing (BESH) plan** — billed at the real-time spot rate rather than a fixed rate. This makes automated charge/discharge decisions genuinely valuable: the difference between charging at -2¢ vs 14¢ on a large battery bank is significant.

## What's Been Built So Far

A Python tkinter dashboard (`ecoflow_dashboard.py`, ~1400 lines) that:
- **Monitors** the battery system live via MQTT telemetry (fully working)
- **Displays** power flow diagram, 15-min history chart, ComEd price sparkline with trend line
- **Has a full automation engine** that decides charge/discharge/hold based on ComEd price + battery SOC
- **Has manual controls** — mode switch buttons, charge rate slider, start/stop charge buttons
- **Has a DRY RUN / LIVE toggle** in the top bar for safe testing

**The one unsolved piece:** The manual and automated controls send commands that the broker accepts (rc=0) but the device doesn't act on. Cracking the correct command format is the immediate next step before the automation can go live.

## What This Project Is (Technical)

A Python dashboard (`ecoflow_dashboard.py`) that monitors and controls a home battery system, automating charging based on ComEd real-time electricity pricing. The dashboard is working and live — telemetry displays correctly. **The unsolved problem is sending commands that the device actually responds to.**

---

## Hardware

| Device | Serial Number | Role |
|--------|--------------|------|
| Smart Home Panel 2 (HR65) | `HR65ZA1AVH7J0027` | Gateway — main control point |
| Delta Pro Ultra Inverter | `P101ZA1A9HA70164` | Inverter — charges through gateway |
| Battery modules | (8 units) | Storage |

**Important:** Commands should target the **gateway only** (`HR65ZA1AVH7J0027`). The inverter charges through the gateway — sending commands directly to the inverter risks power flow conflicts.

---

## MQTT Connection

```
Broker:    mqtt.ecoflow.com:8883  (TLS)
MQTT_USER: app-740f41d44de04eaf83832f8a801252e9
MQTT_PASS: c1e46f17f6994a1e8252f1e1f3135b68
CLIENT_ID: ANDROID_666188426_1971363830522871810
USER_ID:   666188426   (extracted from CLIENT_ID)
```

**Critical:** The broker only allows one connection per CLIENT_ID. Close the dashboard before running any test scripts.

Credentials are also stored in `ecoflow_credentials.txt` next to the scripts in the same folder.

### Topics

```python
# Telemetry (device → us, read-only)
"/app/device/property/HR65ZA1AVH7J0027"
"/app/device/property/P101ZA1A9HA70164"

# Commands (us → device)
"/app/666188426/HR65ZA1AVH7J0027/thing/property/set"
```

---

## Telemetry Format — CONFIRMED WORKING

The device sends **protobuf**, double-nested:

```
outer[field 1] → inner[field 1] → actual data fields
```

### Key telemetry fields (confirmed):

| Field | Type | Meaning |
|-------|------|---------|
| `f518` | float32 | Battery watts (+charging, −discharging) |
| `f1544` | varint | Home load watts (reliable in all modes) |
| `f1063` | float32 | Line A voltage |
| `f1064` | float32 | Line B voltage |
| `f1009.4` | varint | Operating mode (2=self-powered, absent=backup) |

Grid watts = `f1544 + f518` (calculated, not a direct field).

### Working decoder (Python):

```python
def decode_varint(data, pos):
    r, s = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80): break
        s += 7
    return r, pos

def decode_fields(data, prefix=""):
    """Returns flat dict of all fields, keyed by dotted path e.g. '1.1.518'"""
    out = {}
    pos = 0
    while pos < len(data):
        try:
            tag, pos = decode_varint(data, pos)
            fn = tag >> 3; wt = tag & 7
            if fn == 0: break
            key = f"{prefix}{fn}"
            if wt == 0:
                v, pos = decode_varint(data, pos)
                out[key] = v
            elif wt == 2:
                ln, pos = decode_varint(data, pos)
                raw = data[pos:pos+ln]; pos += ln
                out.update(decode_fields(raw, f"{key}."))
            elif wt == 5:
                import struct
                v = struct.unpack_from('<f', data, pos)[0]; pos += 4
                out[key] = round(v, 2)
            else:
                break
        except:
            break
    return out
```

---

## Command Format — THE UNSOLVED PROBLEM

### What we know for certain:
- Commands must be sent as **protobuf** (not JSON — JSON was silently ignored)
- The broker **accepts** commands (publish rc=0) but the device doesn't respond
- Commands go to topic: `/app/666188426/HR65ZA1AVH7J0027/thing/property/set`

### Current protobuf command structure (our best guess):

```python
def build_command(gateway_sn, cmd_func, pdata, seq=42):
    payload = pb_str(1, gateway_sn) + pb_int(2, cmd_func) + pb_int(3, seq) + pb_msg(4, pdata)
    inner   = pb_msg(1, payload) + pb_int(2, seq) + pb_int(3, 19)
    return pb_msg(1, inner)
```

Where `pb_str/pb_int/pb_msg` are standard protobuf encoding helpers.

### cmd_func codes tried so far (ALL FAILED to produce device response):
`11, 16, 20, 32, 40, 50, 64, 69, 85, 96, 136, 254`

### pdata layouts tried:
- `f1=watts, f2=pause_flag` (pause=0 start, pause=1 stop)
- `f1=watts only`
- `f3=watts, f4=pause`

### Example command hex that was sent (accepted by broker, ignored by device):
```
# Charge 1000W, func=11
0a250a1e0a10485236355a4131415648374a30303237100b18db72220508e807100010db721813
```

Decoded structure of that command:
```
field[1] msg:
  field[1] msg:
    field[1] str = 'HR65ZA1AVH7J0027'
    field[2] int = 11       ← cmd_func
    field[3] int = 14683    ← seq
    field[4] msg:
      field[1] int = 1000   ← watts
      field[2] int = 0      ← pause=0 (run)
  field[2] int = 14683      ← seq (outer)
  field[3] int = 19         ← version?
```

### Hypotheses for why commands aren't working:
1. **Wrong cmd_func** — the HR65/SHP2 uses different codes than Delta Pro
2. **Missing required pdata fields** — maybe needs a timestamp, checksum, or reserved bytes
3. **Wrong nesting structure** — maybe the outer wrapper is different for commands vs telemetry
4. **Missing outer envelope** — some devices expect an additional JSON wrapper around the protobuf
5. **Wrong topic** — maybe commands need a different topic structure for this device
6. **ACK/handshake required** — device may require a prior subscribe/ack before accepting commands

### What hasn't been tried yet:
- Checking EcoFlow's developer API docs (api-e.ecoflow.com) for SHP2-specific command structure
- Sniffing the actual app traffic with a MITM proxy (mitmproxy/Charles)
- Checking if there's a `set_reply` topic that gives error feedback
- Trying cmd_func values 1-10, 12-15, 17-19, 21-31, 33-39, 41-49
- Trying a completely different outer structure (no version=19, different nesting)

---

## Dashboard Status

The dashboard (`ecoflow_dashboard.py`) is fully functional for monitoring:
- Real-time power flow diagram
- 15-minute history chart
- ComEd real-time pricing (5-min and hour average)
- Price sparkline with trend line
- Automation engine (decides charge/discharge/hold based on price + SOC)
- Manual controls (mode switch, charge rate slider, start/stop buttons)
- Commands toggle (DRY RUN / LIVE) in top bar

**The only broken piece is the actual command execution** — everything sends with rc=0 but the device doesn't respond.

---

## Files

All files in `C:\Users\kmars\Downloads\`:

| File | Purpose |
|------|---------|
| `ecoflow_dashboard.py` | Main dashboard (~1394 lines) |
| `ecoflow_credentials.txt` | MQTT credentials (loaded at runtime) |
| `ecoflow_cmd_test.py` | Command brute-force tester |
| `ecoflow_debug.py` | Minimal connection + telemetry debugger |
| `cmd_test.log` | Output from last command test run |
| `debug.log` | Output from last debug run |

---

## ComEd Pricing API (already integrated in dashboard)

Free, no auth required:

```
# Current 5-minute real-time price
https://hourlypricing.comed.com/api?type=5minutefeed

# Current hour average (what you're actually billed)
https://hourlypricing.comed.com/api?type=currenthouraverage
```

Both return JSON: `[{"millisUTC": "...", "price": "3.2"}, ...]` — price in **cents/kWh**.

### Automation thresholds (user-configurable in dashboard UI):

| Threshold | Default | Meaning |
|-----------|---------|---------|
| Discharge >= | 9.6¢ | Above ComEd fixed rate → run from battery |
| Stop charging >= | 6.0¢ | Price rising → stop charging |
| Charge normal < | 6.0¢ | Cheap enough → charge at normal rate |
| Charge max < | -0.5¢ | Negative price → charge at max rate |

The automation uses **hour average as primary signal** (billing rate) with 5-minute price as a trend lookahead. SOC overrides: emergency charge below 20%, hold at target SOC (80%), top-off only at deeply negative prices.

---

## Suggested Next Steps for Claude Code

1. **Run `ecoflow_debug.py`** to confirm connection works and see raw telemetry field dump
2. **Check EcoFlow developer API** at `https://developer.ecoflow.com` for SHP2 command docs
3. **Try MITM approach** — run mitmproxy locally, configure phone to proxy through it, use EcoFlow app, capture exact commands
4. **Expand cmd_func sweep** — try every integer from 1-255 systematically
5. **Try alternate outer structures** — no version field, different nesting depth, JSON+protobuf hybrid

## Key Insight

The telemetry decode works perfectly which means our protobuf helpers are correct. The command structure is almost certainly close — most likely just the wrong `cmd_func` integer or a missing field in `pdata`. One successful command will unlock the whole thing.

---

## Development Platform & Future Deployment

**Current development:** Windows (Python + tkinter). All development should stay on Windows until the app is fully working and stable.

**Final deployment goal:** Once the Windows version is complete and proven, port the app to run on an **always-on device** such as:
- Raspberry Pi (Linux) — headless or with small display
- Android tablet — always-on wall-mounted display

The port will require replacing the tkinter UI with something cross-platform (likely a web-based UI served locally, or a React Native / Kivy app for Android). The MQTT/automation logic is pure Python and should port without changes.

**Do not start the port until the Windows version is fully working.**

---

## Supporting Scripts

### ecoflow_cmd_test.py
Systematically tries every plausible cmd_func code and pdata layout against the gateway,
watching telemetry for battery watts to change as confirmation. Run with dashboard closed.
Results saved to `cmd_test.log`.

```python
import os, struct, sys, time, traceback

# ── Log file setup — do this FIRST before anything else ──────────────────────
_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cmd_test.log")
_log_f    = open(_log_path, "w", buffering=1)

def log(msg=""):
    line = str(msg)
    sys.__stdout__.write(line + "\n")
    sys.__stdout__.flush()
    _log_f.write(line + "\n")
    _log_f.flush()

log("=== EcoFlow Command Tester v5 starting ===")
log(f"Log: {_log_path}")

# ── Imports with error reporting ──────────────────────────────────────────────
try:
    import paho.mqtt.client as mqtt
    log("paho-mqtt imported OK")
    # Check which API versions are available
    try:
        ver = mqtt.CallbackAPIVersion.VERSION1
        log(f"CallbackAPIVersion.VERSION1 available")
        USE_V1 = True
    except AttributeError:
        log("CallbackAPIVersion not available — using legacy API")
        USE_V1 = False
except ImportError as e:
    log(f"FATAL: cannot import paho.mqtt: {e}")
    log("Run:  pip install paho-mqtt")
    input("Press Enter to close...")
    sys.exit(1)

# ── Credentials ───────────────────────────────────────────────────────────────
def _load_creds():
    _dir = os.path.dirname(os.path.abspath(__file__))
    c = {"MQTT_USER": "app-740f41d44de04eaf83832f8a801252e9",
         "MQTT_PASS": "c1e46f17f6994a1e8252f1e1f3135b68",
         "CLIENT_ID": "ANDROID_666188426_1971363830522871810"}
    f = os.path.join(_dir, "ecoflow_credentials.txt")
    if os.path.exists(f):
        for line in open(f).read().splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() in c: c[k.strip()] = v.strip()
        log(f"Credentials loaded from {f}")
    return c

creds     = _load_creds()
MQTT_USER = creds["MQTT_USER"]
MQTT_PASS = creds["MQTT_PASS"]
CLIENT_ID = creds["CLIENT_ID"]
USER_ID   = CLIENT_ID.split("_")[1]

GATEWAY_SN = "HR65ZA1AVH7J0027"
MQTT_HOST  = "mqtt.ecoflow.com"
MQTT_PORT  = 8883

TELEMETRY_TOPICS = [
    f"/app/device/property/{GATEWAY_SN}",
    f"/app/device/property/P101ZA1A9HA70164",
]
CMD_TOPIC = f"/app/{USER_ID}/{GATEWAY_SN}/thing/property/set"

log(f"USER_ID:   {USER_ID}")
log(f"CMD_TOPIC: {CMD_TOPIC}")

# ── Protobuf builder ──────────────────────────────────────────────────────────
def _vi(v):
    out = []
    while True:
        out.append(v & 0x7F); v >>= 7
        if v == 0: break
    for i in range(len(out) - 1): out[i] |= 0x80
    return bytes(out)

def pb_str(n, v): t=_vi((n<<3)|2); b=v.encode(); return t+_vi(len(b))+b
def pb_int(n, v): return _vi((n<<3)|0) + _vi(v)
def pb_msg(n, b): t=_vi((n<<3)|2); return t+_vi(len(b))+b

def build(func, pdata, seq=42):
    payload = pb_str(1, GATEWAY_SN) + pb_int(2, func) + pb_int(3, seq) + pb_msg(4, pdata)
    inner   = pb_msg(1, payload) + pb_int(2, seq) + pb_int(3, 19)
    return pb_msg(1, inner)

# ── Decoder ───────────────────────────────────────────────────────────────────
def _dvi(data, pos):
    r, s = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80): break
        s += 7
    return r, pos

def decode_all(data, depth=0, out=None, prefix=""):
    if out is None: out = {}
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
                decode_all(raw, depth+1, out, f"{key}.")
            elif wt == 5:
                v = struct.unpack_from('<f', data, pos)[0]; pos += 4
                out[key] = round(v, 2)
            else:
                break
        except: break
    return out

# ── Global state updated by telemetry ─────────────────────────────────────────
state = {"batt": None, "load": None, "msgs": 0, "raw_fields": {}}

def on_message(client, userdata, msg):
    try:
        state["msgs"] += 1
        fields = decode_all(msg.payload)
        # Save first message fields for debug
        if state["msgs"] == 1:
            state["raw_fields"] = fields

        # Find battery watts — f518 is a float in nested structure
        for k, v in fields.items():
            if str(k).endswith("518") and isinstance(v, float):
                if -10000 < v < 10000:
                    state["batt"] = round(v, 1)
            # Home load f1544 is a varint
            if str(k).endswith("1544") and isinstance(v, int):
                if 0 < v < 30000:
                    state["load"] = v
    except Exception as e:
        log(f"  [msg handler error: {e}]")

# ── Build MQTT client ─────────────────────────────────────────────────────────
try:
    if USE_V1:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                             client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
    else:
        client = mqtt.Client(client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
    log("MQTT client created OK")
except Exception as e:
    log(f"FATAL creating MQTT client: {e}")
    traceback.print_exc()
    input("Press Enter to close...")
    sys.exit(1)

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

# ── Connect ───────────────────────────────────────────────────────────────────
try:
    log(f"\nConnecting to {MQTT_HOST}:{MQTT_PORT}...")
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
except Exception as e:
    log(f"FATAL connect error: {e}")
    traceback.print_exc()
    input("Press Enter to close...")
    sys.exit(1)

log("Waiting 3s for connection...")
time.sleep(3)
if not connected[0]:
    log("ERROR: Not connected after 3s")
    input("Press Enter to close...")
    sys.exit(1)

log("Collecting baseline telemetry for 12s...")
time.sleep(12)
log(f"Baseline: batt={state['batt']}W  load={state['load']}W  msgs={state['msgs']}")

# ── Debug: print all decoded fields from first message ────────────────────────
log("\nFields decoded from first telemetry message:")
if state["raw_fields"]:
    for k, v in sorted(state["raw_fields"].items(), key=lambda x: str(x[0])):
        log(f"  [{k}] = {v}")
else:
    log("  (none — no messages received)")

# ── Command tests ─────────────────────────────────────────────────────────────
tests = [
    ("func=11  f1=watts,f2=pause",   11,  pb_int(1,1000)+pb_int(2,0)),
    ("func=11  f1=watts only",        11,  pb_int(1,1000)),
    ("func=16  f1=watts,f2=pause",   16,  pb_int(1,1000)+pb_int(2,0)),
    ("func=20  f1=watts,f2=pause",   20,  pb_int(1,1000)+pb_int(2,0)),
    ("func=40  f1=watts,f2=pause",   40,  pb_int(1,1000)+pb_int(2,0)),
    ("func=50  f1=watts,f2=pause",   50,  pb_int(1,1000)+pb_int(2,0)),
    ("func=64  f1=watts,f2=pause",   64,  pb_int(1,1000)+pb_int(2,0)),
    ("func=69  f1=watts,f2=pause",   69,  pb_int(1,1000)+pb_int(2,0)),
    ("func=85  f1=watts,f2=pause",   85,  pb_int(1,1000)+pb_int(2,0)),
    ("func=96  f1=watts,f2=pause",   96,  pb_int(1,1000)+pb_int(2,0)),
    ("func=136 f1=watts,f2=pause",  136,  pb_int(1,1000)+pb_int(2,0)),
    ("func=11  f3=watts,f4=pause",   11,  pb_int(3,1000)+pb_int(4,0)),
    ("func=32  f1=watts,f2=pause",   32,  pb_int(1,1000)+pb_int(2,0)),
    ("func=254 f1=watts,f2=pause",  254,  pb_int(1,1000)+pb_int(2,0)),
]

results   = []
found_one = False

for label, func, pdata in tests:
    if found_one: break
    log(f"\n{'─'*50}")
    log(f"TEST: {label}")
    payload = build(func, pdata)
    log(f"HEX:  {payload.hex()}")

    batt_before = state["batt"]
    try:
        rc = client.publish(CMD_TOPIC, payload, qos=1)
        log(f"Published rc={rc.rc}")
    except Exception as e:
        log(f"Publish error: {e}")
        results.append((label, False, batt_before, None, None))
        continue

    log("Watching 10s:")
    for i in range(10):
        time.sleep(1)
        cur_batt = state["batt"]
        cur_load = state["load"]
        delta = round(cur_batt - batt_before, 1) if (cur_batt and batt_before) else None
        marker = "  ◄ CHANGED!" if (delta and abs(delta) > 80) else ""
        log(f"  [{i+1:2d}s] batt={cur_batt}W  load={cur_load}W  Δ={delta}W{marker}")

    final_batt = state["batt"]
    delta = round(final_batt - batt_before, 1) if (final_batt and batt_before) else None
    worked = delta is not None and delta > 80
    results.append((label, worked, batt_before, final_batt, delta))

    if worked:
        found_one = True
        log(f"\n★★★  WORKED: func={func}  batt {batt_before}→{final_batt}W  ★★★")
        stop = build(func, pb_int(1,0)+pb_int(2,1))
        client.publish(CMD_TOPIC, stop, qos=1)
        log("Stop command sent.")
        time.sleep(3)

# ── Summary ───────────────────────────────────────────────────────────────────
log(f"\n{'='*50}")
log("SUMMARY")
log(f"{'='*50}")
for label, worked, bb, ba, delta in results:
    log(f"{'★ WORKED' if worked else '  ------'}  {label}  {bb}→{ba}W  Δ={delta}W")
log(f"\nTotal telemetry msgs: {state['msgs']}")
log(f"Log: {_log_path}")

client.loop_stop()
client.disconnect()
_log_f.flush()
input("\nPress Enter to close...")

```

---

### ecoflow_debug.py
Minimal connection + telemetry field dumper. Use this first to confirm connection works
and to see every decoded field from live telemetry. Results saved to `debug.log`.

```python
import os
import struct
import time

log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug.log")
log_f = open(log_path, "w", buffering=1)

def L(s=""):
    print(s)
    log_f.write(str(s) + "\n")
    log_f.flush()

L("step 1: file open OK")

try:
    import paho.mqtt.client as mqtt
    L("step 2: paho imported OK, version=" + str(getattr(mqtt, '__version__', 'unknown')))
except Exception as e:
    L("step 2 FAILED: " + str(e))
    input("Press Enter...")
    raise

L("step 3: loading credentials")
_dir = os.path.dirname(os.path.abspath(__file__))
_cred_file = os.path.join(_dir, "ecoflow_credentials.txt")
MQTT_USER = "app-740f41d44de04eaf83832f8a801252e9"
MQTT_PASS = "c1e46f17f6994a1e8252f1e1f3135b68"
CLIENT_ID = "ANDROID_666188426_1971363830522871810"
if os.path.exists(_cred_file):
    for line in open(_cred_file).read().splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        if "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "MQTT_USER": MQTT_USER = v
            if k == "MQTT_PASS": MQTT_PASS = v
            if k == "CLIENT_ID": CLIENT_ID = v
    L("credentials loaded from file")
else:
    L("credentials file not found, using defaults")
L("CLIENT_ID=" + CLIENT_ID)
L("MQTT_USER=" + MQTT_USER)

L("step 4: building MQTT client")
try:
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID)
        L("step 4: used VERSION1 API")
    except:
        c = mqtt.Client(client_id=CLIENT_ID)
        L("step 4: used legacy API")
except Exception as e:
    L("step 4 FAILED: " + str(e))
    input("Press Enter...")
    raise

L("step 5: setting credentials")
c.username_pw_set(MQTT_USER, MQTT_PASS)
c.tls_set()

msgs_received = [0]
fields_dump   = [None]

def on_connect(client, userdata, flags, rc):
    L("on_connect rc=" + str(rc))
    if rc == 0:
        client.subscribe("/app/device/property/HR65ZA1AVH7J0027", qos=1)
        client.subscribe("/app/device/property/P101ZA1A9HA70164", qos=1)
        L("subscribed to telemetry topics")

def on_message(client, userdata, msg):
    msgs_received[0] += 1
    n = msgs_received[0]
    L("msg #" + str(n) + " topic=" + msg.topic + " len=" + str(len(msg.payload)))
    if n == 1:
        L("  hex (first 80 bytes): " + msg.payload[:80].hex())
        # Try to decode fields
        try:
            fields = {}
            pos = 0
            data = msg.payload
            def dvi(d, p):
                r, s = 0, 0
                while p < len(d):
                    b = d[p]; p += 1
                    r |= (b & 0x7F) << s
                    if not (b & 0x80): break
                    s += 7
                return r, p
            def decode(d, pfx=""):
                p = 0
                while p < len(d):
                    try:
                        tag, p = dvi(d, p)
                        fn = tag >> 3; wt = tag & 7
                        if fn == 0: break
                        k = pfx + str(fn)
                        if wt == 0:
                            v, p = dvi(d, p)
                            fields[k] = v
                        elif wt == 2:
                            ln, p = dvi(d, p)
                            raw = d[p:p+ln]; p += ln
                            decode(raw, k + ".")
                        elif wt == 5:
                            v = struct.unpack_from('<f', d, p)[0]; p += 4
                            fields[k] = round(v, 2)
                        else:
                            break
                    except:
                        break
            decode(data)
            fields_dump[0] = fields
            L("  decoded fields:")
            for k in sorted(fields.keys(), key=str):
                v = fields[k]
                if isinstance(v, (int, float)):
                    L("    [" + str(k) + "] = " + str(v))
        except Exception as e:
            L("  decode error: " + str(e))

c.on_connect = on_connect
c.on_message = on_message

L("step 6: connecting...")
try:
    c.connect("mqtt.ecoflow.com", 8883, keepalive=60)
    c.loop_start()
except Exception as e:
    L("step 6 FAILED: " + str(e))
    input("Press Enter...")
    raise

L("waiting 20s for messages...")
for i in range(20):
    time.sleep(1)
    L("  t=" + str(i+1) + "s  msgs=" + str(msgs_received[0]))
    if msgs_received[0] >= 2:
        break

L("done. msgs=" + str(msgs_received[0]))
L("log: " + log_path)
c.loop_stop()
c.disconnect()
input("Press Enter to close...")

```

---

### ecoflow_sniffer.py
Subscribes to all topics for the gateway and logs every MQTT message. Useful for
capturing what the official EcoFlow app sends when you toggle things manually.
Run with dashboard closed.

```python
"""
EcoFlow MQTT Sniffer
====================
Subscribe to ALL topics for your gateway and log every message.

Run this, then use the EcoFlow app to:
  1. Change the operating mode (Backup → Self-Powered → back)
  2. Start a charge at a specific rate
  3. Stop the charge

This will capture the exact MQTT topic and payload the app uses,
which we can then replicate in the dashboard.

Run:
  python ecoflow_sniffer.py
"""

import json
import os
import struct
import time
import paho.mqtt.client as mqtt

# ── Load credentials from same file as dashboard ─────────────────────────────
def _load_credentials():
    _dir      = os.path.dirname(os.path.abspath(__file__))
    cred_file = os.path.join(_dir, "ecoflow_credentials.txt")
    creds = {
        "MQTT_USER": "app-740f41d44de04eaf83832f8a801252e9",
        "MQTT_PASS": "c1e46f17f6994a1e8252f1e1f3135b68",
        "CLIENT_ID": "ANDROID_666188426_1971363830522871810",
    }
    if os.path.exists(cred_file):
        for line in open(cred_file).read().splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k in creds:
                    creds[k] = v
        print(f"Credentials loaded from {cred_file}")
    return creds

creds     = _load_credentials()
MQTT_USER = creds["MQTT_USER"]
MQTT_PASS = creds["MQTT_PASS"]
CLIENT_ID = creds["CLIENT_ID"]
USER_ID   = CLIENT_ID.split("_")[1] if CLIENT_ID.count("_") >= 2 else "0"

GATEWAY_SN  = "HR65ZA1AVH7J0027"
INVERTER_SN = "P101ZA1A9HA70164"

MQTT_HOST = "mqtt.ecoflow.com"
MQTT_PORT = 8883

# Subscribe to everything — wildcard catches any topic structure EcoFlow uses
TOPICS = [
    # Catch ALL messages for this user (commands, replies, status)
    f"/app/{USER_ID}/#",
    # Catch ALL device property messages
    f"/app/device/property/{GATEWAY_SN}",
    f"/app/device/property/{INVERTER_SN}",
    # Some EcoFlow firmwares use this structure instead
    f"/{GATEWAY_SN}/#",
    f"/{INVERTER_SN}/#",
]

log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mqtt_sniff.log")

def decode_varint(data, pos):
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80): break
        shift += 7
    return result, pos

def decode_message(data):
    """Shallow protobuf decode — just enough to show field numbers and values."""
    fields = {}
    pos = 0
    while pos < len(data):
        try:
            tag, pos = decode_varint(data, pos)
            field_num = tag >> 3
            wire_type = tag & 0x07
            if field_num == 0: break
            if wire_type == 0:
                val, pos = decode_varint(data, pos)
                fields[field_num] = val
            elif wire_type == 2:
                length, pos = decode_varint(data, pos)
                raw = data[pos: pos + length]; pos += length
                # Try to decode nested, fall back to hex
                try:
                    nested = decode_message(raw)
                    fields[field_num] = nested if nested else raw.hex()
                except:
                    fields[field_num] = raw.hex()
            elif wire_type == 5:
                val = struct.unpack_from("<f", data, pos)[0]; pos += 4
                fields[field_num] = round(val, 4)
            else:
                fields[field_num] = f"[unknown wire_type={wire_type}]"
                break
        except Exception as e:
            fields["_error"] = str(e)
            break
    return fields

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"\n✓ Connected to {MQTT_HOST}")
        print(f"  Subscribing to {len(TOPICS)} topics...\n")
        for t in TOPICS:
            client.subscribe(t, qos=1)
            print(f"  SUB: {t}")
        print()
        print("=" * 70)
        print("NOW USE THE ECOFLOW APP TO:")
        print("  1. Switch mode: Backup → Self-Powered → Backup")
        print("  2. Start AC charging (any rate)")
        print("  3. Stop AC charging")
        print("Watching for command topics (non-telemetry messages)...")
        print("=" * 70)
        print()
    else:
        print(f"✗ Connect failed rc={rc}")

msg_count = 0

def on_message(client, userdata, msg):
    global msg_count
    msg_count += 1
    ts = time.strftime("%H:%M:%S")
    topic = msg.topic
    payload = msg.payload

    # Log ALL messages — we want to catch everything
    is_telemetry = "device/property" in topic and len(payload) > 200

    if is_telemetry:
        # Large telemetry blobs — just print a dot
        print(".", end="", flush=True)
    else:
        # Everything else — print in full
        print(f"\n{'='*60}")
        print(f"[{ts}] #{msg_count} TOPIC: {topic}")
        print(f"  Raw bytes ({len(payload)}): {payload[:120].hex()}{'...' if len(payload)>120 else ''}")

        # Try JSON first
        try:
            parsed = json.loads(payload)
            print(f"  JSON: {json.dumps(parsed, indent=2)}")
        except:
            # Try protobuf
            try:
                decoded = decode_message(payload)
                print(f"  Protobuf: {json.dumps(decoded, indent=2, default=str)}")
            except:
                print(f"  Raw: {payload!r}")

    # Write EVERYTHING to log file (including telemetry blobs)
    with open(log_file, "a") as f:
        f.write(f"\n[{ts}] TOPIC: {topic}  ({len(payload)} bytes)\n")
        f.write(f"  Hex: {payload[:120].hex()}{'...' if len(payload)>120 else ''}\n")
        try:
            f.write(f"  JSON: {json.dumps(json.loads(payload))}\n")
        except:
            try:
                f.write(f"  Proto: {json.dumps(decode_message(payload), default=str)}\n")
            except:
                f.write(f"  Raw: {payload[:200]!r}\n")

# Must use the exact CLIENT_ID — EcoFlow broker ties auth to client ID.
# Close the dashboard before running this or the connection will bounce.
client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION1,
    client_id=CLIENT_ID,
    protocol=mqtt.MQTTv311,
)
client.username_pw_set(MQTT_USER, MQTT_PASS)
client.tls_set()
client.on_connect = on_connect
client.on_message = on_message

print(f"\nEcoFlow MQTT Sniffer")
print(f"NOTE: Close the dashboard first — same CLIENT_ID, broker only allows one connection.")
print(f"Gateway:  {GATEWAY_SN}")
print(f"User ID:  {USER_ID}")
print(f"Log file: {log_file}")
print(f"\nConnecting to {MQTT_HOST}:{MQTT_PORT}...")

client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

try:
    client.loop_forever()
except KeyboardInterrupt:
    print(f"\n\nStopped. Captured {msg_count} messages.")
    print(f"Log saved to: {log_file}")

```

---

## Current Dashboard Source Code

`ecoflow_dashboard.py` — the full working dashboard as of the handoff point:

```python
"""
EcoFlow Home Energy Dashboard  v2.0
=====================================
Full-featured desktop dashboard for EcoFlow Smart Gateway + Delta Pro Ultra,
with ComEd BESH real-time pricing and automated charge/discharge control.

Features:
  • Animated real-time power flow diagram
  • ComEd 5-minute + hourly pricing with trend indicator and sparkline
  • Automated charge/discharge decisions based on price thresholds
  • Adjustable automation thresholds via UI (no code editing)
  • Manual mode + charge controls (DRY RUN by default)
  • 15-minute scrolling history graph
  • Credential file — update ecoflow_credentials.txt when creds change

Dependencies:
  pip install paho-mqtt

Run:
  python ecoflow_dashboard.py
"""

import json
import logging
import math
import os as _os
import struct
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
import tkinter as tk
from tkinter import font as tkfont
import paho.mqtt.client as mqtt


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
MQTT_HOST   = "mqtt.ecoflow.com"
MQTT_PORT   = 8883
GATEWAY_SN  = "HR65ZA1AVH7J0027"
INVERTER_SN = "P101ZA1A9HA70164"

# Set True once you've verified commands work — START WITH False
ENABLE_COMMANDS = False

HISTORY_SECONDS    = 900   # 15 minutes of power history
HISTORY_POINTS     = 180   # one sample every 5s
COMED_POLL_SECONDS = 300   # ComEd publishes a new 5-min price every 5 minutes

# ComEd fixed-rate comparison (Price to Compare as of Jan 2026)
COMED_FIXED_RATE = 9.6    # cents/kWh

# ── Credential file loader ────────────────────────────────────────────────────
def _load_credentials():
    _dir      = _os.path.dirname(_os.path.abspath(__file__))
    cred_file = _os.path.join(_dir, "ecoflow_credentials.txt")
    creds = {
        "MQTT_USER": "app-740f41d44de04eaf83832f8a801252e9",
        "MQTT_PASS": "c1e46f17f6994a1e8252f1e1f3135b68",
        "CLIENT_ID": "ANDROID_666188426_1971363830522871810",
    }
    if _os.path.exists(cred_file):
        for line in open(cred_file).read().splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k in creds:
                    creds[k] = v
        print(f"  Credentials loaded from {cred_file}")
    else:
        with open(cred_file, "w") as f:
            f.write("# EcoFlow MQTT Credentials\n")
            f.write("# When credentials change, update these three lines and restart.\n")
            f.write("# Get new values: EcoFlow app -> Me -> IoT Developer -> MQTT Credentials\n")
            f.write(f"MQTT_USER={creds['MQTT_USER']}\n")
            f.write(f"MQTT_PASS={creds['MQTT_PASS']}\n")
            f.write(f"CLIENT_ID={creds['CLIENT_ID']}\n")
        print(f"  Created credential file: {cred_file}")
    return creds

_creds    = _load_credentials()
MQTT_USER = _creds["MQTT_USER"]
MQTT_PASS = _creds["MQTT_PASS"]
CLIENT_ID = _creds["CLIENT_ID"]
USER_ID   = CLIENT_ID.split("_")[1] if CLIENT_ID.count("_") >= 2 else "0"

TELEMETRY_TOPICS = [
    f"/app/device/property/{GATEWAY_SN}",
    f"/app/device/property/{INVERTER_SN}",
]
COMMAND_TOPIC = f"/app/{USER_ID}/{GATEWAY_SN}/thing/property/set"


# ─────────────────────────────────────────────────────────────────────────────
# COLOUR PALETTE
# ─────────────────────────────────────────────────────────────────────────────
C = {
    "bg":            "#0d1117",
    "panel":         "#161b22",
    "panel2":        "#1c2333",
    "border":        "#30363d",
    "text":          "#e6edf3",
    "dim":           "#8b949e",
    "amber":         "#f0a500",
    "amber_dim":     "#7a5200",
    "green":         "#3fb950",
    "green_dim":     "#1a4a22",
    "red":           "#f85149",
    "red_dim":       "#4a1a1a",
    "blue":          "#58a6ff",
    "blue_dim":      "#1a3a6a",
    "purple":        "#bc8cff",
    "gold":          "#d4a017",
    "grid_line":     "#21262d",
    "flow_grid":     "#f0a500",
    "flow_batt_ch":  "#3fb950",
    "flow_batt_dis": "#f85149",
    "flow_load":     "#58a6ff",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dashboard")


# ─────────────────────────────────────────────────────────────────────────────
# PROTOBUF DECODER
# ─────────────────────────────────────────────────────────────────────────────
class ProtoDecoder:
    @staticmethod
    def decode_varint(data: bytes, pos: int):
        result, shift = 0, 0
        while pos < len(data):
            b = data[pos]; pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80): break
            shift += 7
        return result, pos

    @staticmethod
    def decode_message(data: bytes) -> dict:
        fields = {}
        pos = 0
        while pos < len(data):
            if pos >= len(data): break
            tag, pos = ProtoDecoder.decode_varint(data, pos)
            field_num = tag >> 3
            wire_type = tag & 0x07
            if field_num == 0: break
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
            if not isinstance(entry, dict): return None
            node = entry.get("_nested", {})
        val = node.get(path[-1])
        if val is None: return None
        try: return float(val)
        except: return None

    @staticmethod
    def get_int(fields: dict, *path: int) -> Optional[int]:
        v = ProtoDecoder.get_float(fields, *path)
        return int(v) if v is not None else None


# ─────────────────────────────────────────────────────────────────────────────
# PROTOBUF COMMAND ENCODER
# ─────────────────────────────────────────────────────────────────────────────
class ProtoEncoder:
    """
    Builds protobuf-encoded command messages for the EcoFlow HR65 gateway.
    The command structure mirrors the telemetry nesting:
      outer(field1) -> inner(field1=payload, field2=seq, field3=19)
        payload: field1=device_sn, field2=cmd_func, field3=seq, field4=pdata
    
    cmd_func codes confirmed for HR65:
      11 = AC charge config  (pdata: field1=watts, field2=pause_flag)
      32 = Work mode         (pdata: field1=mode  1=backup 2=self-powered)
    """

    @staticmethod
    def _varint(v: int) -> bytes:
        out = []
        while True:
            out.append(v & 0x7F)
            v >>= 7
            if v == 0: break
        for i in range(len(out) - 1):
            out[i] |= 0x80
        return bytes(out)

    @staticmethod
    def _field(num: int, wire: int, val) -> bytes:
        e = ProtoEncoder
        tag = e._varint((num << 3) | wire)
        if wire == 0: return tag + e._varint(val)
        if wire == 2: return tag + e._varint(len(val)) + val
        if wire == 5: return tag + struct.pack('<f', val)
        raise ValueError(f"Unknown wire type {wire}")

    @classmethod
    def _str(cls, num, s):   return cls._field(num, 2, s.encode())
    @classmethod
    def _int(cls, num, v):   return cls._field(num, 0, v)
    @classmethod
    def _msg(cls, num, b):   return cls._field(num, 2, b)

    @classmethod
    def charge_config(cls, device_sn: str, watts: int, pause: int = 0,
                      seq: int = 1) -> bytes:
        """AC charge config command. pause=0 to start, pause=1 to stop."""
        pdata   = cls._int(1, watts) + cls._int(2, pause)
        payload = (cls._str(1, device_sn) + cls._int(2, 11) +
                   cls._int(3, seq)       + cls._msg(4, pdata))
        inner   = cls._msg(1, payload) + cls._int(2, seq) + cls._int(3, 19)
        return cls._msg(1, inner)

    @classmethod
    def work_mode(cls, device_sn: str, mode: int, seq: int = 1) -> bytes:
        """Work mode command. mode=1 backup, mode=2 self-powered."""
        pdata   = cls._int(1, mode)
        payload = (cls._str(1, device_sn) + cls._int(2, 32) +
                   cls._int(3, seq)       + cls._msg(4, pdata))
        # BUG FIX: outer seq must match inner seq (was hardcoded 1 before)
        inner   = cls._msg(1, payload) + cls._int(2, seq) + cls._int(3, 19)
        return cls._msg(1, inner)

    @classmethod
    def build(cls, device_sn: str, cmd_func: int, pdata: bytes,
              seq: int = 1) -> bytes:
        """Generic command builder — for trying alternate cmd_func codes."""
        payload = (cls._str(1, device_sn) + cls._int(2, cmd_func) +
                   cls._int(3, seq)       + cls._msg(4, pdata))
        inner   = cls._msg(1, payload) + cls._int(2, seq) + cls._int(3, 19)
        return cls._msg(1, inner)


# ─────────────────────────────────────────────────────────────────────────────
# APPLICATION STATE
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class PowerState:
    grid_w:      Optional[float] = None
    load_w:      Optional[float] = None
    battery_w:   Optional[float] = None   # + charging, - discharging
    volt_a:      Optional[float] = None
    volt_b:      Optional[float] = None
    op_mode:     Optional[int]   = None   # 1=backup, 2=self-powered
    soc_pct:     Optional[float] = None
    last_update: float           = 0.0

    @property
    def battery_charging(self):    return (self.battery_w or 0) > 50
    @property
    def battery_discharging(self): return (self.battery_w or 0) < -50
    @property
    def mode_label(self):          return {1: "BACKUP", 2: "SELF-POWERED"}.get(self.op_mode, "—")
    @property
    def stale(self):               return (time.time() - self.last_update) > 10


@dataclass
class PriceState:
    price_5min:   Optional[float] = None   # cents/kWh, most recent 5-min interval
    price_hour:   Optional[float] = None   # cents/kWh, current hour average
    trend:        str             = "flat" # "rising" | "falling" | "flat"
    trend_slope:  float           = 0.0
    tier:         str             = "—"
    tier_color:   str             = "#8b949e"
    history_5min: list            = field(default_factory=list)  # [(ts, price), ...] newest first
    last_update:  float           = 0.0
    error:        str             = ""

    @property
    def stale(self): return self.last_update > 0 and (time.time() - self.last_update) > 180


def parse_payload(payload: bytes, state: PowerState) -> bool:
    try:
        outer   = ProtoDecoder.decode_message(payload)
        level1  = outer.get(1)
        if not isinstance(level1, dict): return False
        nested1 = level1.get("_nested", {})
        inner   = nested1.get(1)
        data    = inner.get("_nested", {}) if isinstance(inner, dict) else nested1
        if not data: return False

        gf = lambda *p: ProtoDecoder.get_float(data, *p)
        gi = lambda *p: ProtoDecoder.get_int(data, *p)
        updated = False

        def _s(attr, val):
            nonlocal updated
            if val is not None:
                setattr(state, attr, val)
                updated = True

        _s("battery_w", gf(518))
        _s("volt_a",    gf(1063))
        _s("volt_b",    gf(1064))

        # f1544 = reliable home load in ALL modes (f515 drops to 0 during discharge)
        load = gi(1544)
        if load is not None and load > 0:
            _s("load_w", float(load))

        # Grid = Home + Battery (signed). No direct grid field exists in telemetry.
        batt = gf(518)
        if load is not None and batt is not None:
            _s("grid_w", max(0.0, float(load) + batt))
        elif load is not None:
            _s("grid_w", float(load))

        # Mode: f1009 sub[4]=2 → Self-Powered; sub[4] absent → Backup
        m = data.get(1009)
        if isinstance(m, dict):
            sub4 = m.get("_nested", {}).get(4)
            if isinstance(sub4, int) and sub4 == 2:
                _s("op_mode", 2)
            elif sub4 is None:
                _s("op_mode", 1)

        if updated:
            state.last_update = time.time()
        return updated
    except Exception as e:
        log.debug("parse error: %s", e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# HISTORY BUFFER
# ─────────────────────────────────────────────────────────────────────────────
class HistoryBuffer:
    def __init__(self, maxlen=HISTORY_POINTS):
        self.times   = deque(maxlen=maxlen)
        self.grid    = deque(maxlen=maxlen)
        self.load    = deque(maxlen=maxlen)
        self.battery = deque(maxlen=maxlen)
        self._last   = 0.0

    def maybe_add(self, state: PowerState):
        now = time.time()
        if now - self._last < 5.0: return
        self._last = now
        self.times.append(now)
        self.grid.append(state.grid_w or 0)
        self.load.append(state.load_w or 0)
        self.battery.append(state.battery_w or 0)


# ─────────────────────────────────────────────────────────────────────────────
# COMED PRICE ENGINE
# ─────────────────────────────────────────────────────────────────────────────
COMED_5MIN_URL    = "https://hourlypricing.comed.com/api?type=5minutefeed"
COMED_HOURAVG_URL = "https://hourlypricing.comed.com/api?type=currenthouraverage"


def _classify_price(cents: float) -> tuple:
    """Return (tier_label, hex_color) for a price in cents/kWh."""
    if cents < 0:                    return "NEGATIVE",  "#00ff88"
    elif cents < 3:                  return "VERY LOW",  "#3fb950"
    elif cents < 6:                  return "LOW",       "#58a6ff"
    elif cents < COMED_FIXED_RATE:   return "MODERATE",  "#f0a500"
    elif cents < 14:                 return "HIGH",      "#f85149"
    else:                            return "SPIKE",     "#ff4040"


def _price_trend(entries: list, n=6) -> tuple:
    """
    entries: [(timestamp, price_cents), ...] newest first.
    Returns (direction_str, slope_float) — positive slope = rising over time.
    """
    if len(entries) < 2: return "flat", 0.0
    # Reverse slice to get chronological order for regression
    chrono = [p for _, p in reversed(entries[:n])]
    n_pts  = len(chrono)
    xm     = (n_pts - 1) / 2
    ym     = sum(chrono) / n_pts
    num    = sum((i - xm) * (chrono[i] - ym) for i in range(n_pts))
    den    = sum((i - xm) ** 2 for i in range(n_pts))
    slope  = num / den if den else 0.0
    if slope > 0.3:    return "rising",  round(slope, 3)
    elif slope < -0.3: return "falling", round(slope, 3)
    else:              return "flat",    round(slope, 3)


class ComedPoller:
    """Polls ComEd APIs in a background thread every COMED_POLL_SECONDS."""

    def __init__(self, price_state: PriceState, on_update):
        self.ps         = price_state
        self.on_update  = on_update
        self._stop      = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        self._poll()
        while not self._stop.wait(COMED_POLL_SECONDS):
            self._poll()

    def _poll(self):
        try:
            with urllib.request.urlopen(COMED_5MIN_URL, timeout=10) as r:
                raw_5min = json.loads(r.read())
            with urllib.request.urlopen(COMED_HOURAVG_URL, timeout=10) as r:
                raw_hour = json.loads(r.read())

            entries = sorted(
                [(int(x["millisUTC"]) / 1000.0, float(x["price"])) for x in raw_5min],
                key=lambda e: e[0], reverse=True
            )
            if not entries: return

            current   = entries[0][1]
            hour_avg  = float(raw_hour[0]["price"]) if raw_hour else None
            trend, sl = _price_trend(entries)
            tier, col = _classify_price(current)

            self.ps.price_5min   = current
            self.ps.price_hour   = hour_avg
            self.ps.trend        = trend
            self.ps.trend_slope  = sl
            self.ps.tier         = tier
            self.ps.tier_color   = col
            self.ps.history_5min = entries[:12]
            self.ps.last_update  = time.time()
            self.ps.error        = ""

            log.info("ComEd: 5min=%.1f¢  hour=%s¢  trend=%s(%.2f)  tier=%s",
                     current,
                     f"{hour_avg:.1f}" if hour_avg else "?",
                     trend, sl, tier)
            self.on_update()

        except Exception as e:
            self.ps.error = str(e)
            log.warning("ComEd poll failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# AUTOMATION THRESHOLDS & CONTROLLER
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class AutoThresholds:
    discharge_above:    float = 9.6    # switch to Self-Powered above this (¢)
    hold_above:         float = 6.0    # stop charging above this (¢)
    charge_normal:      float = 6.0    # charge at normal rate below this (¢)
    charge_aggressive:  float = 3.0    # charge at max rate below this (¢)
    soc_emergency:      float = 20.0   # always charge regardless of price (%)
    soc_target:         float = 80.0   # normal charge target (%)
    soc_topoff:         float = 95.0   # only top-off at very cheap prices (%)
    rate_normal:        int   = 1500   # normal charge rate (W)
    rate_aggressive:    int   = 3000   # max charge rate (W)
    rate_emergency:     int   = 1000   # emergency charge rate (W)
    trend_lookahead:    bool  = True   # pre-act on strong trends


class AutoController:
    MIN_HOLD = 120   # seconds between commands

    def __init__(self):
        self.enabled        = False
        self.last_mode      = None
        self.last_rate      = None
        self.last_cmd_ts    = 0.0
        self.last_decision  = "—"

    def decide(self, ps: PriceState, pw: PowerState, t: AutoThresholds):
        """
        Returns (target_mode, target_rate_w, reason). None = no change.

        Primary signal: current hour average — this is what you're billed.
        Trend lookahead: 5-min slope nudges the effective price to pre-act
        before the hour average catches up to a move. Weight is halved when
        we have a solid hour average (less aggressive lookahead vs raw 5-min).
        Early in a new hour when hour_avg isn't meaningful yet, falls back to
        5-min price only.
        """
        soc = pw.soc_pct

        # Pick base price — prefer hour average (billing signal)
        if ps.price_hour is not None:
            base = ps.price_hour
            src  = "hr"
        elif ps.price_5min is not None:
            base = ps.price_5min
            src  = "5m"
        else:
            return None, None, "waiting for price data"

        # Trend lookahead using 5-min slope — nudge effective price if
        # 5-min is trending strongly away from current hour average.
        # Halved weight when we have a real hour average (less hair-trigger).
        ep = base
        if t.trend_lookahead and ps.price_5min is not None and abs(ps.trend_slope) > 1.0:
            weight = 0.5 if src == "hr" else 1.0
            ep = max(-5.0, base + ps.trend_slope * 2 * weight)

        # Emergency SOC — charge regardless of price
        if soc is not None and soc < t.soc_emergency:
            return 1, t.rate_emergency, f"EMERGENCY: SOC {soc:.0f}% < {t.soc_emergency:.0f}%"

        # Battery full — just decide on mode
        if soc is not None and soc >= t.soc_topoff:
            if ep >= t.discharge_above:
                return 2, 0, f"DISCHARGE: full + {ep:.1f}c [{src}] >= {t.discharge_above:.1f}c"
            return 1, 0, f"HOLD: battery full ({soc:.0f}%)"

        # High price → self-powered (discharge)
        if ep >= t.discharge_above:
            if soc is None or soc > t.soc_emergency + 10:
                return 2, 0, f"DISCHARGE: {ep:.1f}c [{src}] >= {t.discharge_above:.1f}c"
            return 1, 0, f"HOLD: price high but SOC {soc:.0f}% too low"

        # Hold band — don't charge
        if ep >= t.hold_above:
            return 1, 0, f"HOLD: {ep:.1f}c [{src}] in hold band"

        # Aggressive charging (very cheap)
        if ep < t.charge_aggressive:
            if soc is None or soc < t.soc_topoff:
                label = "TOPOFF" if (soc and soc >= t.soc_target) else "CHARGE MAX"
                return 1, t.rate_aggressive, f"{label}: {ep:.1f}c [{src}] deeply cheap"

        # Normal charging
        if ep < t.charge_normal:
            if soc is None or soc < t.soc_target:
                return 1, t.rate_normal, f"CHARGE: {ep:.1f}c [{src}] < {t.charge_normal:.1f}c"
            return 1, 0, f"HOLD: SOC {soc:.0f}% at target"

        return 1, 0, f"HOLD: {ep:.1f}c [{src}]"

    def should_send(self, target_mode, target_rate):
        if not self.enabled:
            return False, "automation off"
        mode_changed = target_mode is not None and target_mode != self.last_mode
        rate_changed = target_rate is not None and target_rate != self.last_rate
        if not mode_changed and not rate_changed:
            return False, "no change"
        if self.last_cmd_ts > 0:
            elapsed = time.time() - self.last_cmd_ts
            if elapsed < self.MIN_HOLD:
                return False, f"hold {self.MIN_HOLD - elapsed:.0f}s"
        return True, "ok"

    def record(self, target_mode, target_rate, reason):
        if target_mode is not None: self.last_mode = target_mode
        if target_rate is not None: self.last_rate = target_rate
        self.last_cmd_ts   = time.time()
        self.last_decision = reason


# ─────────────────────────────────────────────────────────────────────────────
# MQTT CLIENT
# ─────────────────────────────────────────────────────────────────────────────
class MQTTHandler:
    def __init__(self, state: PowerState, history: HistoryBuffer, on_update):
        self.state       = state
        self.history     = history
        self.on_update   = on_update
        self.connected   = False
        self.last_msg_ts = 0.0
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION1,
            client_id=CLIENT_ID,
            protocol=mqtt.MQTTv311,
        )
        self._client.username_pw_set(MQTT_USER, MQTT_PASS)
        self._client.tls_set()
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        self.connected = (rc == 0)
        if rc == 0:
            log.info("MQTT connected OK")
            for t in TELEMETRY_TOPICS:
                client.subscribe(t, qos=1)
        else:
            log.error("MQTT connect failed rc=%d", rc)

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            log.warning("MQTT disconnected rc=%d", rc)

    def _on_message(self, client, userdata, msg):
        self.last_msg_ts = time.time()
        if parse_payload(msg.payload, self.state):
            self.history.maybe_add(self.state)
            self.on_update()

    def start(self):
        self._client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
        self._client.loop_start()

    def publish_command(self, payload: bytes, commands_live: bool = False):
        """Send a protobuf-encoded command. payload must be bytes from ProtoEncoder."""
        if commands_live:
            rc = self._client.publish(COMMAND_TOPIC, payload, qos=1)
            log.info("CMD LIVE rc=%s  %d bytes: %s", rc.rc, len(payload), payload.hex())
        else:
            log.info("CMD DRY  %d bytes: %s", len(payload), payload.hex())


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────
class Dashboard:
    REFRESH_MS = 250

    def __init__(self):
        self.state      = PowerState()
        self.price      = PriceState()
        self.history    = HistoryBuffer()
        self.thresholds = AutoThresholds()
        self.auto       = AutoController()
        self.mqtt       = MQTTHandler(self.state, self.history, self._on_mqtt_update)
        self.comed      = ComedPoller(self.price, self._on_price_update)
        self._dirty          = False
        self._lock           = threading.Lock()
        self._thresh_vars    = {}     # attr -> (StringVar, update_fn)
        self._commands_live  = False  # toggled via UI button

        self._build_window()
        self.mqtt.start()
        self.comed.start()
        self._tick()

    # ─────────────────────────────────────────────────────────────────────────
    # WINDOW CONSTRUCTION
    # ─────────────────────────────────────────────────────────────────────────
    def _build_window(self):
        self.root = tk.Tk()
        self.root.title("EcoFlow Energy Dashboard  v2.0")
        self.root.configure(bg=C["bg"])
        self.root.geometry("1500x900")
        self.root.minsize(1200, 720)

        try:
            self.fn_mono  = tkfont.Font(family="Consolas", size=11)
            self.fn_sm    = tkfont.Font(family="Consolas", size=9)
            self.fn_big   = tkfont.Font(family="Consolas", size=26, weight="bold")
            self.fn_med   = tkfont.Font(family="Consolas", size=13, weight="bold")
            self.fn_lbl   = tkfont.Font(family="Consolas", size=9)
            self.fn_hdr   = tkfont.Font(family="Consolas", size=10, weight="bold")
        except Exception:
            self.fn_mono  = tkfont.Font(family="Courier", size=11)
            self.fn_sm    = tkfont.Font(family="Courier", size=9)
            self.fn_big   = tkfont.Font(family="Courier", size=26, weight="bold")
            self.fn_med   = tkfont.Font(family="Courier", size=13, weight="bold")
            self.fn_lbl   = tkfont.Font(family="Courier", size=9)
            self.fn_hdr   = tkfont.Font(family="Courier", size=10, weight="bold")

        self._build_topbar()

        body = tk.Frame(self.root, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        self.left = tk.Frame(body, bg=C["bg"])
        self.left.pack(side="left", fill="both", expand=True)

        self.right = tk.Frame(body, bg=C["panel"], width=304)
        self.right.pack(side="right", fill="y", padx=(8, 0))
        self.right.pack_propagate(False)

        self._build_flow_canvas()
        self._build_history_canvas()
        self._build_controls()

    def _build_topbar(self):
        bar = tk.Frame(self.root, bg=C["panel"], height=44)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        tk.Label(bar, text="⚡ ECOFLOW ENERGY", bg=C["panel"],
                 fg=C["amber"], font=self.fn_hdr).pack(side="left", padx=16, pady=8)

        self.btn_cmd_toggle = tk.Button(
            bar, text="COMMANDS: DRY RUN",
            bg=C["amber_dim"], fg=C["amber"],
            font=self.fn_lbl, relief="flat", bd=0,
            cursor="hand2", padx=10, pady=4,
            command=self._toggle_commands
        )
        self.btn_cmd_toggle.pack(side="left", padx=12, pady=6)

        self.lbl_time     = tk.Label(bar, text="--:--:--", bg=C["panel"],
                                      fg=C["dim"], font=self.fn_sm)
        self.lbl_time.pack(side="right", padx=16)

        self.lbl_status   = tk.Label(bar, text="● CONNECTING", bg=C["panel"],
                                      fg=C["amber_dim"], font=self.fn_hdr)
        self.lbl_status.pack(side="right", padx=12)

        self.lbl_mode_top = tk.Label(bar, text="MODE: —", bg=C["panel"],
                                      fg=C["dim"], font=self.fn_hdr)
        self.lbl_mode_top.pack(side="right", padx=20)

        self.lbl_price_top = tk.Label(bar, text="PRICE: —", bg=C["panel"],
                                       fg=C["dim"], font=self.fn_hdr)
        self.lbl_price_top.pack(side="right", padx=20)

    def _build_flow_canvas(self):
        f = tk.Frame(self.left, bg=C["panel"])
        f.pack(fill="both", expand=True, pady=(0, 6))
        tk.Label(f, text="POWER FLOW", bg=C["panel"], fg=C["dim"],
                 font=self.fn_lbl).pack(anchor="nw", padx=10, pady=(6, 0))
        self.flow_canvas = tk.Canvas(f, bg=C["panel"], highlightthickness=0)
        self.flow_canvas.pack(fill="both", expand=True, padx=6, pady=(2, 6))

    def _build_history_canvas(self):
        f = tk.Frame(self.left, bg=C["panel"])
        f.pack(fill="both", expand=True)
        hdr = tk.Frame(f, bg=C["panel"])
        hdr.pack(fill="x", padx=10, pady=(6, 0))
        tk.Label(hdr, text="15-MINUTE HISTORY", bg=C["panel"], fg=C["dim"],
                 font=self.fn_lbl).pack(side="left")
        for lbl, clr in [("GRID", C["flow_grid"]), ("LOAD", C["flow_load"]),
                          ("BATTERY", C["green"])]:
            tk.Label(hdr, text=f"── {lbl}", bg=C["panel"],
                     fg=clr, font=self.fn_lbl).pack(side="right", padx=8)
        self.hist_canvas = tk.Canvas(f, bg=C["panel"], highlightthickness=0)
        self.hist_canvas.pack(fill="both", expand=True, padx=6, pady=(2, 6))

    # ─────────────────────────────────────────────────────────────────────────
    # CONTROLS PANEL
    # ─────────────────────────────────────────────────────────────────────────
    def _build_controls(self):
        p = self.right

        def sep(title):
            tk.Frame(p, bg=C["border"], height=1).pack(fill="x", padx=8, pady=(10, 3))
            tk.Label(p, text=title, bg=C["panel"], fg=C["dim"],
                     font=self.fn_lbl).pack(anchor="w", padx=12)

        tk.Label(p, text="CONTROLS", bg=C["panel"], fg=C["amber"],
                 font=self.fn_hdr).pack(pady=(12, 0), padx=12, anchor="w")

        # ── ComEd Price ──────────────────────────────────────────────────────
        sep("COMED PRICE  (¢/kWh)")

        price_row = tk.Frame(p, bg=C["panel"])
        price_row.pack(fill="x", padx=12, pady=(2, 0))
        self.lbl_price_big = tk.Label(price_row, text="—", bg=C["panel"],
                                       fg=C["dim"], font=self.fn_med)
        self.lbl_price_big.pack(side="left")
        self.lbl_trend = tk.Label(price_row, text="", bg=C["panel"],
                                   fg=C["dim"], font=self.fn_sm)
        self.lbl_trend.pack(side="left", padx=8)

        self.lbl_tier     = tk.Label(p, text="—", bg=C["panel"],
                                      fg=C["dim"], font=self.fn_lbl)
        self.lbl_tier.pack(anchor="w", padx=12)

        self.lbl_hour_avg = tk.Label(p, text="Hour avg: —", bg=C["panel"],
                                      fg=C["dim"], font=self.fn_lbl)
        self.lbl_hour_avg.pack(anchor="w", padx=12)

        self.price_canvas = tk.Canvas(p, bg=C["panel2"], highlightthickness=0, height=56)
        self.price_canvas.pack(fill="x", padx=12, pady=(4, 0))

        # ── Automation ───────────────────────────────────────────────────────
        sep("AUTOMATION")

        self.btn_auto = tk.Button(
            p, text="AUTO: OFF", font=self.fn_hdr,
            bg=C["panel2"], fg=C["dim"], relief="flat", bd=0,
            cursor="hand2", padx=8, pady=5,
            command=self._toggle_automation
        )
        self.btn_auto.pack(fill="x", padx=10, pady=(2, 0))

        self.lbl_auto_status = tk.Label(
            p, text="Enable to auto-control charging / mode",
            bg=C["panel"], fg=C["dim"], font=self.fn_lbl,
            wraplength=276, justify="left"
        )
        self.lbl_auto_status.pack(anchor="w", padx=12, pady=(2, 4))

        # Threshold adjusters
        thresh_f = tk.Frame(p, bg=C["panel"])
        thresh_f.pack(fill="x", padx=10)
        self._thresh_row(thresh_f, "Discharge >=",  "discharge_above",   1.0, 30.0)
        self._thresh_row(thresh_f, "Stop chg >=",   "hold_above",       -5.0, 20.0)
        self._thresh_row(thresh_f, "Chg normal <",  "charge_normal",    -5.0, 15.0)
        self._thresh_row(thresh_f, "Chg max <",     "charge_aggressive",-5.0, 10.0)

        # ── Operating Mode ───────────────────────────────────────────────────
        sep("OPERATING MODE")

        self.lbl_mode = tk.Label(p, text="—", bg=C["panel"],
                                  fg=C["text"], font=self.fn_med)
        self.lbl_mode.pack(pady=(2, 4))

        btn_row = tk.Frame(p, bg=C["panel"])
        btn_row.pack(fill="x", padx=10)
        self.btn_backup = tk.Button(
            btn_row, text="BACKUP", font=self.fn_hdr,
            bg=C["panel2"], fg=C["text"], relief="flat", bd=0,
            cursor="hand2", padx=8, pady=5,
            command=lambda: self._cmd_mode("backup")
        )
        self.btn_backup.pack(side="left", expand=True, fill="x", padx=(0, 3))
        self.btn_selfpow = tk.Button(
            btn_row, text="SELF-PWR", font=self.fn_hdr,
            bg=C["panel2"], fg=C["text"], relief="flat", bd=0,
            cursor="hand2", padx=8, pady=5,
            command=lambda: self._cmd_mode("self_powered")
        )
        self.btn_selfpow.pack(side="left", expand=True, fill="x", padx=(3, 0))

        # ── Battery Charge ───────────────────────────────────────────────────
        sep("BATTERY CHARGE")

        self.lbl_batt_status = tk.Label(p, text="IDLE", bg=C["panel"],
                                         fg=C["dim"], font=self.fn_med)
        self.lbl_batt_status.pack(pady=(2, 2))

        tk.Label(p, text="CHARGE RATE (W)", bg=C["panel"],
                 fg=C["dim"], font=self.fn_lbl).pack(anchor="w", padx=12)

        self.charge_rate_var = tk.IntVar(value=1000)
        self.slider = tk.Scale(
            p, from_=500, to=7200, resolution=100,
            orient="horizontal", variable=self.charge_rate_var,
            bg=C["panel"], fg=C["text"], troughcolor=C["panel2"],
            highlightthickness=0, sliderrelief="flat",
            activebackground=C["amber"], font=self.fn_lbl,
            command=lambda v: self.lbl_rate.config(text=f"{int(float(v))} W")
        )
        self.slider.pack(fill="x", padx=10, pady=(0, 2))

        self.lbl_rate = tk.Label(p, text="1000 W", bg=C["panel"],
                                  fg=C["amber"], font=self.fn_med)
        self.lbl_rate.pack()

        chg_row = tk.Frame(p, bg=C["panel"])
        chg_row.pack(fill="x", padx=10, pady=(4, 0))
        tk.Button(
            chg_row, text="▶ START CHARGE", font=self.fn_hdr,
            bg=C["green_dim"], fg=C["green"], relief="flat", bd=0,
            cursor="hand2", padx=8, pady=6,
            command=self._cmd_charge_start
        ).pack(fill="x", pady=(0, 3))
        tk.Button(
            chg_row, text="■ STOP CHARGE", font=self.fn_hdr,
            bg=C["red_dim"], fg=C["red"], relief="flat", bd=0,
            cursor="hand2", padx=8, pady=6,
            command=self._cmd_charge_stop
        ).pack(fill="x")

        # ── Live Readouts ────────────────────────────────────────────────────
        sep("LIVE READINGS")
        self.readout_vars = {}
        for key, label, unit in [
            ("grid_w",    "Grid Draw",  "W"),
            ("load_w",    "Home Load",  "W"),
            ("battery_w", "Battery",    "W"),
            ("volt_a",    "Line A",     "V"),
            ("volt_b",    "Line B",     "V"),
        ]:
            row = tk.Frame(p, bg=C["panel"])
            row.pack(fill="x", padx=12, pady=1)
            tk.Label(row, text=label, bg=C["panel"], fg=C["dim"],
                     font=self.fn_sm, width=11, anchor="w").pack(side="left")
            var = tk.StringVar(value="—")
            lbl = tk.Label(row, textvariable=var, bg=C["panel"],
                           fg=C["text"], font=self.fn_sm, width=10, anchor="e")
            lbl.pack(side="right")
            self.readout_vars[key] = (var, lbl, unit)

        # ── Command Log ──────────────────────────────────────────────────────
        sep("COMMAND LOG")
        self.cmd_log = tk.Text(
            p, bg=C["bg"], fg=C["dim"], font=self.fn_lbl,
            height=6, wrap="word", state="disabled", relief="flat", padx=4, pady=4
        )
        self.cmd_log.pack(fill="x", padx=10, pady=(2, 10))

    def _thresh_row(self, parent, label, attr, lo, hi):
        """One threshold row: label  [-]  value  [+]"""
        row = tk.Frame(parent, bg=C["panel"])
        row.pack(fill="x", pady=1)
        tk.Label(row, text=label, bg=C["panel"], fg=C["dim"],
                 font=self.fn_lbl, width=14, anchor="w").pack(side="left")

        var = tk.StringVar()

        def refresh():
            var.set(f"{getattr(self.thresholds, attr):.1f}c")

        def decr():
            v = getattr(self.thresholds, attr)
            setattr(self.thresholds, attr, round(max(lo, v - 0.5), 1))
            refresh()

        def incr():
            v = getattr(self.thresholds, attr)
            setattr(self.thresholds, attr, round(min(hi, v + 0.5), 1))
            refresh()

        refresh()
        tk.Button(row, text="-", font=self.fn_lbl, bg=C["panel2"], fg=C["text"],
                  relief="flat", bd=0, width=2, cursor="hand2",
                  command=decr).pack(side="left")
        tk.Label(row, textvariable=var, bg=C["panel"], fg=C["amber"],
                 font=self.fn_lbl, width=7).pack(side="left")
        tk.Button(row, text="+", font=self.fn_lbl, bg=C["panel2"], fg=C["text"],
                  relief="flat", bd=0, width=2, cursor="hand2",
                  command=incr).pack(side="left")

        self._thresh_vars[attr] = refresh

    # ─────────────────────────────────────────────────────────────────────────
    # COMMAND HANDLERS
    # ─────────────────────────────────────────────────────────────────────────
    def _cmd_mode(self, mode: str):
        m = 1 if mode == "backup" else 2
        seq = int(time.time()) & 0xFFFF
        self._log_cmd(f"MODE -> {mode.upper()}  (mode={m}, seq={seq})")
        # Try gateway SN first; if no response try inverter SN in next iteration
        payload = ProtoEncoder.work_mode(GATEWAY_SN, m, seq=seq)
        self.mqtt.publish_command(payload, self._commands_live)

    def _cmd_charge_start(self, rate: int = None):
        rate = rate or self.charge_rate_var.get()
        seq  = int(time.time()) & 0xFFFF
        self._log_cmd(f"CHARGE START  {rate} W  (seq={seq})")
        payload = ProtoEncoder.charge_config(GATEWAY_SN, rate, pause=0, seq=seq)
        self.mqtt.publish_command(payload, self._commands_live)

    def _cmd_charge_stop(self):
        self._log_cmd("CHARGE STOP")
        payload = ProtoEncoder.charge_config(GATEWAY_SN, 0, pause=1,
                                             seq=int(time.time()) & 0xFFFF)
        self.mqtt.publish_command(payload, self._commands_live)

    def _toggle_commands(self):
        self._commands_live = not self._commands_live
        state = "LIVE" if self._commands_live else "DRY RUN"
        log.info("Commands toggled: %s", state)
        self._log_cmd(f"COMMANDS set to {state}")

    def _toggle_automation(self):
        self.auto.enabled = not self.auto.enabled
        self._log_cmd(f"AUTOMATION {'ON' if self.auto.enabled else 'OFF'}")

    def _log_cmd(self, text: str):
        ts     = time.strftime("%H:%M:%S")
        prefix = "[LIVE]" if self._commands_live else "[DRY] "
        self.cmd_log.config(state="normal")
        self.cmd_log.insert("end", f"{ts} {prefix} {text}\n")
        self.cmd_log.see("end")
        lines = int(self.cmd_log.index("end-1c").split(".")[0])
        if lines > 40:
            self.cmd_log.delete("1.0", "2.0")
        self.cmd_log.config(state="disabled")

    # ─────────────────────────────────────────────────────────────────────────
    # CALLBACKS & AUTOMATION
    # ─────────────────────────────────────────────────────────────────────────
    def _on_mqtt_update(self):
        with self._lock: self._dirty = True
        # Re-evaluate automation when power state changes (SOC thresholds)
        if self.auto.enabled and self.price.price_hour is not None:
            threading.Thread(target=self._run_automation, daemon=True).start()

    def _on_price_update(self):
        with self._lock: self._dirty = True
        threading.Thread(target=self._run_automation, daemon=True).start()

    def _run_automation(self):
        try:
            mode, rate, reason = self.auto.decide(
                self.price, self.state, self.thresholds)
            self.auto.last_decision = reason

            ok, why = self.auto.should_send(mode, rate)
            if not ok:
                return

            self._log_cmd(f"AUTO: {reason}")

            if mode == 2:
                self._cmd_mode("self_powered")
            elif mode == 1 and self.state.op_mode == 2:
                self._cmd_mode("backup")

            if rate == 0:
                self._cmd_charge_stop()
            elif rate is not None:
                self._cmd_charge_start(rate=rate)

            self.auto.record(mode, rate, reason)

        except Exception as e:
            log.warning("Automation error: %s", e)

    # ─────────────────────────────────────────────────────────────────────────
    # TICK & DRAWING
    # ─────────────────────────────────────────────────────────────────────────
    def _tick(self):
        with self._lock: self._dirty = False
        self._update_topbar()
        self._update_price_panel()
        self._update_controls()
        self._update_readouts()
        self._draw_flow()
        self._draw_history()
        self.root.after(self.REFRESH_MS, self._tick)

    def _update_topbar(self):
        self.lbl_time.config(text=time.strftime("%H:%M:%S"))

        if self.mqtt.connected and not self.state.stale:
            self.lbl_status.config(text="● LIVE",       fg=C["green"])
        elif self.mqtt.connected:
            self.lbl_status.config(text="● CONNECTED",  fg=C["amber"])
        else:
            self.lbl_status.config(text="● CONNECTING…", fg=C["red"])

        # Commands toggle button
        if self._commands_live:
            self.btn_cmd_toggle.config(
                text="COMMANDS: LIVE", bg=C["green_dim"], fg=C["green"])
        else:
            self.btn_cmd_toggle.config(
                text="COMMANDS: DRY RUN", bg=C["amber_dim"], fg=C["amber"])

        self.lbl_mode_top.config(
            text=f"MODE: {self.state.mode_label}",
            fg=C["green"] if self.state.op_mode == 2 else C["blue"])

        ps = self.price
        if ps.price_hour is not None or ps.price_5min is not None:
            arrow = {"rising": " ↑", "falling": " ↓", "flat": " →"}.get(ps.trend, "")
            # Top bar shows hour avg (billing price) with trend arrow
            primary = ps.price_hour if ps.price_hour is not None else ps.price_5min
            _, top_color = _classify_price(primary)
            self.lbl_price_top.config(
                text=f"PRICE: {primary:.1f}c{arrow}", fg=top_color)
        else:
            self.lbl_price_top.config(text="PRICE: —", fg=C["dim"])

    def _update_price_panel(self):
        ps = self.price
        if ps.price_hour is not None:
            # Hour average is the primary (billing) number — show it big
            hour_tier, hour_color = _classify_price(ps.price_hour)
            self.lbl_price_big.config(text=f"{ps.price_hour:.1f}c", fg=hour_color)
            self.lbl_tier.config(text=f"{hour_tier}  (hour avg)", fg=hour_color)
        elif ps.price_5min is not None:
            hour_color = ps.tier_color
            self.lbl_price_big.config(text=f"{ps.price_5min:.1f}c", fg=ps.tier_color)
            self.lbl_tier.config(text=ps.tier, fg=ps.tier_color)
        else:
            note = f"({ps.error[:28]})" if ps.error else "(polling…)"
            self.lbl_price_big.config(text="—", fg=C["dim"])
            self.lbl_tier.config(text="—", fg=C["dim"])
            self.lbl_trend.config(text=note, fg=C["dim"])
            self.lbl_hour_avg.config(text="—", fg=C["dim"])
            self._draw_price_sparkline()
            return

        # 5-min price is the secondary (real-time) number
        if ps.price_5min is not None:
            arrow = {"rising": "↑ RISING", "falling": "↓ FALLING",
                     "flat":   "→ STABLE"}.get(ps.trend, "")
            self.lbl_trend.config(text=arrow, fg=ps.tier_color)
            self.lbl_hour_avg.config(
                text=f"5-min: {ps.price_5min:.1f}c  ({ps.tier})",
                fg=ps.tier_color)
        else:
            self.lbl_trend.config(text="", fg=C["dim"])
            self.lbl_hour_avg.config(text="5-min: —", fg=C["dim"])

        self._draw_price_sparkline()

    def _draw_price_sparkline(self):
        c = self.price_canvas
        c.delete("all")
        W = c.winfo_width(); H = c.winfo_height()
        if W < 10 or H < 10: return

        entries = self.price.history_5min
        if len(entries) < 2: return

        # entries newest-first; reverse for left→right (chronological)
        vals = [p for _, p in reversed(entries)]
        n    = len(vals)
        lo   = min(vals) - 0.5
        hi   = max(vals) + 0.5
        rng  = max(hi - lo, 2.0)

        def px(i): return int(i / max(n - 1, 1) * (W - 6)) + 3
        def py(v): return int(H - 4 - (v - lo) / rng * (H - 8))

        # Zero line — visible when prices go negative
        if lo < 0 < hi:
            zy = py(0)
            c.create_line(0, zy, W, zy, fill=C["border"], width=1, dash=(2, 4))
            c.create_text(W - 2, zy - 1, text="0", fill=C["dim"],
                          font=self.fn_lbl, anchor="se")

        # Charge-max threshold line (green dashed) — fires below this
        t_agg = self.thresholds.charge_aggressive
        if lo - 1 <= t_agg <= hi + 1:
            c.create_line(0, py(t_agg), W, py(t_agg),
                          fill=C["green_dim"], width=1, dash=(3, 5))

        # Discharge threshold reference line (red dashed)
        thresh = self.thresholds.discharge_above
        if lo - 1 <= thresh <= hi + 1:
            ty = py(thresh)
            c.create_line(0, ty, W, ty, fill=C["red_dim"], width=1, dash=(3, 5))

        # Fixed rate reference line (amber dashed) — the break-even vs fixed plan
        if lo - 1 <= COMED_FIXED_RATE <= hi + 1:
            fy = py(COMED_FIXED_RATE)
            c.create_line(0, fy, W, fy, fill=C["amber_dim"], width=1, dash=(2, 6))

        # Price sparkline (actual data)
        pts  = [(px(i), py(v)) for i, v in enumerate(vals)]
        flat = [coord for pt in pts for coord in pt]
        if len(flat) >= 4:
            c.create_line(flat, fill=self.price.tier_color, width=2, smooth=True)

        # ── Linear regression trend line ─────────────────────────────────────
        # Compute slope and intercept over all points
        if n >= 3:
            xs    = list(range(n))
            xm    = (n - 1) / 2
            ym    = sum(vals) / n
            num   = sum((xs[i] - xm) * (vals[i] - ym) for i in range(n))
            den   = sum((xs[i] - xm) ** 2 for i in range(n))
            slope = num / den if den else 0.0
            # Predicted values at first and last point
            v0    = ym + slope * (0 - xm)
            v1    = ym + slope * ((n - 1) - xm)
            # Clamp to visible range for drawing (line extends full width)
            tx0, ty0 = px(0),     py(v0)
            tx1, ty1 = px(n - 1), py(v1)
            # Trend line color: green=falling (cheaper), red=rising (more expensive)
            if slope > 0.15:
                tline_color = "#f85149"    # rising → red
            elif slope < -0.15:
                tline_color = "#3fb950"    # falling → green
            else:
                tline_color = "#8b949e"    # flat → grey
            c.create_line(tx0, ty0, tx1, ty1,
                          fill=tline_color, width=2, dash=(6, 4))

        # Current price dot (end of sparkline)
        if pts:
            lx, ly = pts[-1]
            c.create_oval(lx-3, ly-3, lx+3, ly+3,
                          fill=self.price.tier_color, outline="")

    def _update_controls(self):
        s = self.state
        # Mode buttons
        if s.op_mode == 1:
            self.btn_backup.config( bg=C["blue_dim"],  fg=C["blue"])
            self.btn_selfpow.config(bg=C["panel2"],    fg=C["dim"])
        elif s.op_mode == 2:
            self.btn_selfpow.config(bg=C["green_dim"], fg=C["green"])
            self.btn_backup.config( bg=C["panel2"],    fg=C["dim"])
        else:
            self.btn_backup.config( bg=C["panel2"],    fg=C["dim"])
            self.btn_selfpow.config(bg=C["panel2"],    fg=C["dim"])

        # Battery status
        bw = s.battery_w or 0
        if bw > 50:
            self.lbl_batt_status.config(text=f"CHARGING  +{bw:.0f}W",   fg=C["green"])
        elif bw < -50:
            self.lbl_batt_status.config(text=f"DISCHARGING  {bw:.0f}W", fg=C["red"])
        else:
            self.lbl_batt_status.config(text="IDLE", fg=C["dim"])

        self.lbl_mode.config(
            text=s.mode_label,
            fg=C["green"] if s.op_mode == 2 else C["blue"])

        # Automation button
        if self.auto.enabled:
            self.btn_auto.config(text="AUTO: ON", bg=C["green_dim"], fg=C["green"])
            self.lbl_auto_status.config(text=self.auto.last_decision, fg=C["text"])
        else:
            self.btn_auto.config(text="AUTO: OFF", bg=C["panel2"], fg=C["dim"])
            self.lbl_auto_status.config(
                text="Enable to auto-control charging / mode", fg=C["dim"])

    def _update_readouts(self):
        s = self.state
        vals = {"grid_w": s.grid_w, "load_w": s.load_w, "battery_w": s.battery_w,
                "volt_a": s.volt_a,  "volt_b": s.volt_b}
        for key, (var, lbl, unit) in self.readout_vars.items():
            v = vals[key]
            if v is None:
                var.set("—"); lbl.config(fg=C["dim"])
            else:
                var.set(f"{v:+.0f} W" if key == "battery_w" else
                        f"{v:.0f} W"  if unit == "W" else f"{v:.1f} V")
                if key == "battery_w":
                    lbl.config(fg=C["green"] if v > 50 else (C["red"] if v < -50 else C["dim"]))
                elif key == "grid_w":
                    lbl.config(fg=C["amber"] if (v or 0) > 100 else C["green"])
                else:
                    lbl.config(fg=C["text"])

    # ─────────────────────────────────────────────────────────────────────────
    # POWER FLOW DIAGRAM
    # ─────────────────────────────────────────────────────────────────────────
    def _draw_flow(self):
        c = self.flow_canvas
        c.delete("all")
        W = c.winfo_width(); H = c.winfo_height()
        if W < 10 or H < 10: return

        s = self.state
        cx, cy   = W / 2, H / 2
        r        = min(W, H) * 0.10
        gx, gy   = cx, cy
        grid_x, grid_y = cx * 0.38, cy * 0.42
        batt_x, batt_y = cx * 0.38, cy * 1.58
        load_x, load_y = cx * 1.62, cy

        for x in range(0, W, 40):
            c.create_line(x, 0, x, H, fill=C["grid_line"])
        for y in range(0, H, 40):
            c.create_line(0, y, W, y, fill=C["grid_line"])

        def flow(x1, y1, x2, y2, watts, color):
            if watts is None or abs(watts) < 10:
                c.create_line(x1, y1, x2, y2, fill=C["border"], width=2, dash=(4, 8))
                return
            spd = min(abs(watts) / 1000.0, 3.0)
            off = int(time.time() * spd * 20) % 20
            if watts < 0: x1, y1, x2, y2 = x2, y2, x1, y1
            c.create_line(x1, y1, x2, y2, fill=color, width=3, dash=(8, 12), dashoffset=off)
            dx, dy = x2 - x1, y2 - y1
            ln = math.hypot(dx, dy)
            if ln > 0:
                ux, uy = dx / ln, dy / ln
                ax, ay = x2 - ux * 18, y2 - uy * 18
                px2, py2 = -uy * 7, ux * 7
                c.create_polygon(x2, y2, ax+px2, ay+py2, ax-px2, ay-py2,
                                  fill=color, outline="")
            mx, my = (x1+x2)/2, (y1+y2)/2
            c.create_text(mx, my - 12, text=f"{abs(watts):.0f} W",
                          fill=color, font=self.fn_sm)

        gw = s.grid_w or 0; bw = s.battery_w or 0; lw = s.load_w or 0
        bc = C["green"] if bw > 50 else (C["red"] if bw < -50 else C["dim"])

        flow(grid_x, grid_y, gx, gy, gw, C["flow_grid"])
        flow(batt_x, batt_y, gx, gy, -bw, bc)
        flow(gx, gy, load_x, load_y, lw, C["flow_load"])

        def node(x, y, label, sub, color, icon):
            c.create_oval(x-r-8, y-r-8, x+r+8, y+r+8, fill="", outline=color, width=1)
            c.create_oval(x-r,   y-r,   x+r,   y+r,   fill=C["panel2"], outline=color, width=2)
            c.create_text(x, y-8,  text=icon,  fill=color, font=self.fn_med)
            c.create_text(x, y+10, text=label, fill=color, font=self.fn_lbl)
            c.create_text(x, y+22, text=sub,   fill=C["dim"], font=self.fn_lbl)

        gdir = "IMPORT" if gw > 0 else ("EXPORT" if gw < 0 else "IDLE")
        bdir = "CHG" if bw > 50 else ("DSG" if bw < -50 else "IDLE")
        node(grid_x, grid_y, "GRID",    gdir,          C["flow_grid"], "⚡")
        node(batt_x, batt_y, "BATTERY", bdir,          bc,             "🔋")
        node(load_x, load_y, "HOME",    f"{lw:.0f}W",  C["flow_load"], "🏠")

        # Gateway centre
        gwc = C["amber"] if not s.stale else C["dim"]
        r2  = r * 1.3
        c.create_oval(gx-r2-8, gy-r2-8, gx+r2+8, gy+r2+8, fill="", outline=gwc, width=1)
        c.create_oval(gx-r2,   gy-r2,   gx+r2,   gy+r2,   fill=C["panel2"], outline=gwc, width=2)
        c.create_text(gx, gy-10, text="⚙",       fill=gwc, font=self.fn_big)
        c.create_text(gx, gy+20, text="GATEWAY",  fill=gwc, font=self.fn_lbl)
        mc = C["green"] if s.op_mode == 2 else C["blue"]
        c.create_text(gx, gy+32, text=s.mode_label, fill=mc, font=self.fn_lbl)

        # ComEd price overlay — hour avg (billing) primary, 5-min secondary
        ps = self.price
        if ps.price_hour is not None or ps.price_5min is not None:
            arr = {"rising": "↑", "falling": "↓", "flat": "→"}.get(ps.trend, "")
            primary = ps.price_hour if ps.price_hour is not None else ps.price_5min
            _, ov_color = _classify_price(primary)
            line1 = f"ComEd: {primary:.1f}c {arr}"
            c.create_text(W-10, 10, text=line1,
                          fill=ov_color, font=self.fn_lbl, anchor="ne")
            if ps.price_5min is not None and ps.price_hour is not None:
                c.create_text(W-10, 22,
                              text=f"5-min: {ps.price_5min:.1f}c",
                              fill=C["dim"], font=self.fn_lbl, anchor="ne")

        if s.volt_a and s.volt_b:
            c.create_text(W-10, H-10,
                          text=f"L1 {s.volt_a:.1f}V  L2 {s.volt_b:.1f}V",
                          fill=C["dim"], font=self.fn_lbl, anchor="se")

    # ─────────────────────────────────────────────────────────────────────────
    # HISTORY GRAPH
    # ─────────────────────────────────────────────────────────────────────────
    def _draw_history(self):
        c = self.hist_canvas
        c.delete("all")
        W = c.winfo_width(); H = c.winfo_height()
        if W < 10 or H < 10: return

        PL, PR, PT, PB = 50, 12, 10, 30
        gw = W - PL - PR; gh = H - PT - PB

        c.create_rectangle(PL, PT, PL+gw, PT+gh, fill=C["bg"], outline=C["border"])

        hist = self.history
        if len(hist.times) < 2:
            c.create_text(PL+gw/2, PT+gh/2, text="Collecting data…",
                          fill=C["dim"], font=self.fn_lbl)
            return

        now  = time.time()
        t0   = now - HISTORY_SECONDS
        all_v = list(hist.grid) + list(hist.load) + list(hist.battery)
        vmin = min(all_v) if all_v else -500
        vmax = max(all_v) if all_v else 5000
        rng  = max(vmax - vmin, 500)
        vmin -= rng * 0.05; vmax += rng * 0.05

        def tx(t): return PL + (t - t0) / HISTORY_SECONDS * gw
        def ty(v): return PT + gh - (v - vmin) / (vmax - vmin) * gh

        for frac in [0.0, 0.25, 0.5, 0.75, 1.0]:
            y = PT + gh * (1 - frac)
            v = vmin + (vmax - vmin) * frac
            c.create_line(PL, y, PL+gw, y, fill=C["grid_line"])
            c.create_text(PL-4, y, text=f"{v:.0f}", fill=C["dim"],
                          font=self.fn_lbl, anchor="e")

        if vmin < 0 < vmax:
            c.create_line(PL, ty(0), PL+gw, ty(0), fill=C["border"], dash=(4, 4))

        for m in [0, 3, 6, 9, 12, 15]:
            x = tx(now - m * 60)
            if PL <= x <= PL+gw:
                c.create_text(x, PT+gh+12, text=f"-{m}m" if m else "now",
                              fill=C["dim"], font=self.fn_lbl, anchor="n")

        times = list(hist.times)

        def series(vals, color):
            pts  = [(tx(t), ty(v)) for t, v in zip(times, vals) if tx(t) >= PL]
            if len(pts) < 2: return
            c.create_line([coord for pt in pts for coord in pt],
                          fill=color, width=2, smooth=True)

        series(hist.grid,    C["flow_grid"])
        series(hist.load,    C["flow_load"])
        series(hist.battery, C["green"])

        # Clip
        for r2 in [(0, 0, PL-1, H), (PL+gw+1, 0, W, H),
                   (0, 0, W, PT-1), (0, PT+gh+1, W, H)]:
            c.create_rectangle(*r2, fill=C["panel"], outline=C["panel"])

    def run(self):
        self.root.mainloop()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print()
    print("  EcoFlow Energy Dashboard  v2.0")
    print("  Commands: DRY RUN  (use COMMANDS button in top bar to go live)")
    print()
    Dashboard().run()

## Reference: EcoFlow Protocol Documentation
- `references/ef-ble-reverse/` contains reverse-engineered BLE/protobuf protocol definitions for EcoFlow DPU and SHP2
- EcoFlow newer devices (including Smart Gateway, DPU, SHP2) use protobuf over MQTT, NOT JSON
- The .proto files in this reference define the message structure we need to decode/encode
- See also: tolwi/hassio-ecoflow-cloud for ecopacket.proto, platform.proto, powerstream.proto

```
