"""
EcoFlow Grid Charge Command Test v2
=====================================
Key new insight: The phone app sends 83-byte HEARTBEAT messages at ~1/sec
CONTINUOUSLY on the SET topic. These use a different inner structure
(field9=19, field10=4, no field127 pdata) versus charge commands (field9=17).

Theory: The device only accepts commands from a session that is sending
heartbeats, just like the real phone app does. This test:
  1. Sends heartbeats for 5s to "register" with the device
  2. Then sends the charge ON command (interleaved with heartbeats)
  3. Waits, then sends charge OFF

Heartbeat structure (83 bytes, reconstructed from sniffer):
  outer.field1 = inner (81 bytes)
    inner.field1 = sub_msg { field1 = bytes(0x81, 0x01) }  ← 6 bytes
    inner.field2  = 32
    inner.field3  = 11
    inner.field4  = 1
    inner.field5  = 1
    inner.field8  = 254
    inner.field9  = 19   ← heartbeat marker (vs 17 for commands)
    inner.field10 = 4    ← heartbeat marker (vs pdata_len+3 for commands)
    inner.field11 = 1
    inner.field14 = seq  ← single byte (keep < 128 to match 83-byte size)
    inner.field15 = 96
    inner.field16 = 4
    inner.field17 = 1
    inner.field23 = "Android"
    inner.field26 = SN
    inner.field27 = SN

Also: uses seq=1 for ON, seq=2 for OFF (single-byte varint → 102/110 bytes).
"""

import json, os, struct, time, threading, urllib.request
import paho.mqtt.client as mqtt

_dir = os.path.dirname(os.path.abspath(__file__))
creds = {}
for line in open(os.path.join(_dir, "ecoflow_credentials.txt")).read().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        creds[k.strip()] = v.strip()

MQTT_USER  = creds.get("MQTT_USER", "")
MQTT_PASS  = creds.get("MQTT_PASS", "")
REST_JWT   = creds.get("REST_JWT", "")
BASE_ID    = creds.get("CLIENT_ID", "ANDROID_696905537_1971363830522871810")
_p         = BASE_ID.split("_", 2)
SESSION_ID = _p[2]
_rand      = int(_p[1])
TEST_ID    = f"ANDROID_{_rand + 11}_{SESSION_ID}"

SN        = "HR65ZA1AVH7J0027"
MQTT_HOST = "mqtt.ecoflow.com"
MQTT_PORT = 8883

SET_TOPIC  = f"/app/{SESSION_ID}/{SN}/thing/property/set"
TELE_TOPIC = f"/app/device/property/{SN}"
GET_TOPIC  = f"/app/{SESSION_ID}/{SN}/thing/property/get"
GET_MSG    = json.dumps({"from": "HomeAssistant", "id": "999", "version": "1.1",
                         "moduleType": 0, "operateType": "latestQuotas", "params": {}})

# ── Protobuf ──────────────────────────────────────────────────────────────────
def vi(v):
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

def pb(n, w, v):
    t = vi((n << 3) | w)
    if w == 0: return t + vi(v)
    if w == 2: return t + vi(len(v)) + v
    if w == 5: return t + struct.pack('<f', v)


def _common_tail(seq, sn=SN):
    """Fields that are the same in heartbeats and commands."""
    return (
        pb(2,  0, 32)              +
        pb(3,  0, 11)              +
        pb(4,  0, 1)               +
        pb(5,  0, 1)               +
        pb(8,  0, 254)             +
        pb(11, 0, 1)               +
        pb(14, 0, seq)             +
        pb(15, 0, 96)              +
        pb(16, 0, 4)               +
        pb(17, 0, 1)               +
        pb(23, 2, b"Android")      +
        pb(26, 2, sn.encode())     +
        pb(27, 2, sn.encode())
    )


def build_heartbeat(seq, sn=SN):
    """
    83-byte heartbeat, reconstructed from sniffer captures.
    Sent every ~1 second by the phone app.
    """
    # field1 inner_f1 = 6 bytes: 0a 04 0a 02 81 01
    inner_f1 = pb(1, 2, pb(1, 2, b'\x81\x01'))   # field1(field1(bytes))
    inner = (
        inner_f1                   +
        _common_tail(seq, sn)      +
        pb(9,  0, 19)              +   # field9=19  HEARTBEAT marker
        pb(10, 0, 4)                   # field10=4  HEARTBEAT marker
    )
    return pb(1, 2, inner)


def build_charge_command(pdata, seq, sn=SN):
    """
    102-byte (ON) or 110-byte (OFF) charge command.
    Seq must be < 128 for correct sizing.
    """
    f127  = pb(127, 2, pdata)              # field127 wrapper
    inner = (
        pb(1,  2, f127)            +   # field1 = sub-msg{field127=pdata}
        _common_tail(seq, sn)      +
        pb(9,  0, 17)              +   # field9=17  COMMAND marker
        pb(10, 0, len(pdata) + 3)      # field10=len(f127)
    )
    return pb(1, 2, inner)


def pdata_on(watts=500.0):
    sub = pb(1, 0, 4) + pb(2, 5, watts)
    return (
        pb(2, 0, 1)     +
        pb(5, 0, 1)     +
        pb(6, 0, 127)   +
        pb(7, 0, 12345678) +
        pb(10, 2, sub)
    )


def pdata_off(watts=500.0):
    sub = pb(1, 0, 4) + pb(2, 5, watts)
    return (
        pb(1, 0, 0xFFFFFFFF) +
        pb(2, 0, 1)          +
        pb(3, 0, 1)          +
        pb(5, 0, 1)          +
        pb(6, 0, 127)        +
        pb(7, 0, 12345679)   +
        pb(10, 2, sub)
    )


# ── REST mode switch ──────────────────────────────────────────────────────────
def set_mode(target_mode):
    if not REST_JWT:
        print("  [REST] No JWT in credentials — skipping")
        return {}
    url  = "https://api-a.ecoflow.com/tou-service/goe/ai-mode/notify-mode-changed"
    hdrs = {
        "Authorization": f"Bearer {REST_JWT}",
        "Content-Type": "application/json",
        "lang": "en-us", "countryCode": "US",
        "platform": "android", "version": "6.11.0.1731",
        "User-Agent": "okhttp/4.11.0", "X-Appid": "-1",
    }
    body = json.dumps({"sn": SN, "systemNo": "", "targetMode": target_mode}).encode()
    req  = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


# ── Telemetry parsing ─────────────────────────────────────────────────────────
def dv(d, p):
    r, s = 0, 0
    while p < len(d):
        b = d[p]; p += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80):
            break
        s += 7
    return r, p


def decode_telemetry(payload):
    """Decode pdata from standard telemetry message."""
    fields = {}
    try:
        tag, pos = dv(payload, 0)
        if (tag >> 3) != 1 or (tag & 7) != 2:
            return fields
        ln, pos = dv(payload, pos)
        inner = payload[pos:pos+ln]
        # Scan inner for field1 (pdata) and field515/1544 floats
        ipos = 0
        while ipos < len(inner):
            try:
                itag, ipos = dv(inner, ipos)
                ifn = itag >> 3; iwt = itag & 7
                if iwt == 0:
                    iv, ipos = dv(inner, ipos)
                    if ifn == 1544:
                        fields["home_w"] = iv
                elif iwt == 2:
                    iln, ipos = dv(inner, ipos)
                    chunk = inner[ipos:ipos+iln]; ipos += iln
                    if ifn == 1:
                        # decode pdata
                        pp = 0
                        while pp < len(chunk):
                            try:
                                ptag, pp = dv(chunk, pp)
                                pfn = ptag >> 3; pwt = ptag & 7
                                if pwt == 0:
                                    pv, pp = dv(chunk, pp)
                                elif pwt == 2:
                                    pln, pp = dv(chunk, pp)
                                    pchunk = chunk[pp:pp+pln]; pp += pln
                                    if pfn == 518 and pln == 4:
                                        fields["bat_w"] = struct.unpack("<f", pchunk)[0]
                                    elif pfn == 1009:
                                        # mode/SOC sub-msg
                                        sp = 0
                                        while sp < len(pchunk):
                                            stag, sp = dv(pchunk, sp)
                                            sfn = stag >> 3; swt = stag & 7
                                            if swt == 0:
                                                sv, sp = dv(pchunk, sp)
                                                if sfn == 4:
                                                    mmap = {1:"BACKUP", 2:"SELF-POWERED"}
                                                    fields["mode"] = mmap.get(sv, sv)
                                                elif sfn == 5:
                                                    fields["soc"] = sv
                                            else:
                                                break
                                elif pwt == 5:
                                    pv = struct.unpack_from("<f", chunk, pp)[0]; pp += 4
                                    if pfn == 515:
                                        fields["grid_w"] = pv
                                else:
                                    break
                            except:
                                break
                elif iwt == 5:
                    iv = struct.unpack_from("<f", inner, ipos)[0]; ipos += 4
                    if ifn == 515:
                        fields["grid_w"] = iv
                else:
                    break
            except:
                break
    except:
        pass
    return fields


# ── MQTT session ──────────────────────────────────────────────────────────────
connected    = threading.Event()
tele_log     = []
tele_lock    = threading.Lock()
seq_counter  = [10]   # start at 10 (heartbeat seq), increment per message
stop_hb      = threading.Event()
mqtt_client  = [None]

def on_connect(c, u, f, rc):
    if rc == 0:
        print(f"  MQTT connected as {TEST_ID}")
        c.subscribe(TELE_TOPIC, qos=0)
        connected.set()
    else:
        print(f"  MQTT connect FAILED rc={rc}")

def on_message(c, u, msg):
    if len(msg.payload) < 20:
        return
    fields = decode_telemetry(msg.payload)
    if not fields:
        return
    ts = time.strftime("%H:%M:%S")
    parts = []
    if "bat_w" in fields:  parts.append(f"Battery={fields['bat_w']:+.1f}W")
    if "home_w" in fields: parts.append(f"Home={fields['home_w']}W")
    if "grid_w" in fields: parts.append(f"Grid={fields['grid_w']:.0f}W")
    if "soc"    in fields: parts.append(f"SOC={fields['soc']}%")
    if "mode"   in fields: parts.append(f"Mode={fields['mode']}")
    if parts:
        line = " | ".join(parts)
        with tele_lock:
            tele_log.append((ts, line))
        print(f"  [{ts}] {line}")

def heartbeat_thread():
    """Send 83-byte heartbeats every 1 second, just like the phone app."""
    cli = mqtt_client[0]
    while not stop_hb.is_set():
        s = seq_counter[0] % 127 + 1   # keep < 128
        seq_counter[0] += 1
        hb = build_heartbeat(seq=s)
        if len(hb) != 83:
            print(f"  [HB] WARNING size={len(hb)} expected 83")
        cli.publish(SET_TOPIC, hb, qos=0)   # heartbeats use qos=0 like the app
        stop_hb.wait(timeout=1.0)

def send_charge_cmd(label, pdata_bytes, seq):
    cli = mqtt_client[0]
    cmd = build_charge_command(pdata_bytes, seq=seq)
    rc, mid = cli.publish(SET_TOPIC, cmd, qos=1)
    print(f"\n  [{label}] Sent {len(cmd)}b  rc={rc}  seq={seq}")
    print(f"  HEX: {cmd.hex()}")
    return rc


# ── Verify sizes ──────────────────────────────────────────────────────────────
_hb_test = build_heartbeat(seq=1)
_on_test  = build_charge_command(pdata_on(), seq=1)
_off_test = build_charge_command(pdata_off(), seq=2)
print("=" * 65)
print("Size verification:")
print(f"  Heartbeat: {len(_hb_test)}b  (expected 83)")
print(f"  Charge ON: {len(_on_test)}b  (expected 102)")
print(f"  Charge OFF:{len(_off_test)}b (expected 110)")
if len(_hb_test) == 83 and len(_on_test) == 102 and len(_off_test) == 110:
    print("  All sizes CORRECT [OK]")
else:
    print("  *** SIZE MISMATCH — check encoder! ***")
print()
print(f"HB hex:  {_hb_test.hex()}")
print(f"ON hex:  {_on_test.hex()}")
print(f"OFF hex: {_off_test.hex()}")
print()
# Compare heartbeat against captured reference
ref_hb = "0a510a040a0281011020180b2001280140fe01481350045801704a7860800104880101ba0107416e64726f6964d20110485236355a4131415648374a30303237"
our_hb_prefix = _hb_test.hex()[:len(ref_hb)]
print(f"Ref HB first 63 bytes: {ref_hb}")
print(f"Our HB first 63 bytes: {our_hb_prefix}")
match_count = sum(1 for a,b in zip(ref_hb, our_hb_prefix) if a==b)
print(f"Hex char match: {match_count}/{len(ref_hb)} ({100*match_count//len(ref_hb)}%)")

import sys
confirm = input("\nSizes OK? Press ENTER to run test, Ctrl+C to abort: ")

# ── Connect ───────────────────────────────────────────────────────────────────
print("=" * 65)
print("EcoFlow Grid Charge Command Test v2 (with heartbeats)")
print(f"SESSION_ID: {SESSION_ID}")
print(f"TEST_ID:    {TEST_ID}")
print(f"SET topic:  {SET_TOPIC}")
print("=" * 65)

try:
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                      client_id=TEST_ID, protocol=mqtt.MQTTv311)
except AttributeError:
    cli = mqtt.Client(client_id=TEST_ID, protocol=mqtt.MQTTv311)
cli.username_pw_set(MQTT_USER, MQTT_PASS)
cli.tls_set()
cli.on_connect = on_connect
cli.on_message = on_message
mqtt_client[0] = cli

print("\nConnecting to MQTT...")
cli.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
cli.loop_start()
if not connected.wait(timeout=10):
    print("  MQTT connect timeout — aborting")
    sys.exit(1)

# Set REST mode to Backup first
print("\n[STEP 1] Setting REST Backup mode (targetMode=-1)...")
r = set_mode(-1)
print(f"  Result: {r}")

# Get baseline
print("\n[STEP 2] Baseline telemetry...")
cli.publish(GET_TOPIC, GET_MSG, qos=1)
time.sleep(4)

# Start heartbeats
print("\n[STEP 3] Starting heartbeats (sending 83-byte keepalives at 1/sec)...")
hb_thread = threading.Thread(target=heartbeat_thread, daemon=True)
hb_thread.start()
time.sleep(5)
print("  (5 heartbeats sent)")

# Charge ON
print("\n[STEP 4] Sending CHARGE ON command (seq=1, 500W)...")
send_charge_cmd("CHARGE ON", pdata_on(500.0), seq=1)
print("Watching 25s for battery watts changing (should go POSITIVE)...")
cli.publish(GET_TOPIC, GET_MSG, qos=1)
time.sleep(25)

# Charge OFF
print("\n[STEP 5] Sending CHARGE OFF command (seq=2, 500W)...")
send_charge_cmd("CHARGE OFF", pdata_off(500.0), seq=2)
print("Watching 15s...")
cli.publish(GET_TOPIC, GET_MSG, qos=1)
time.sleep(15)

stop_hb.set()
cli.loop_stop()

print("\n" + "=" * 65)
print("DONE — Telemetry during test:")
with tele_lock:
    for ts, line in tele_log:
        print(f"  [{ts}] {line}")
print("=" * 65)
