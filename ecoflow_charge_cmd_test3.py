"""
EcoFlow Grid Charge Command Test v3
=====================================
CRITICAL FIX: Field order must match exactly.

Reference byte stream (charge ON, first 45 bytes):
  0a64                            outer f1 len=100
  0a17 fa0714 [20-byte pdata]     inner.f1 = sub{f127=pdata}
  1020 180b 2001 2801              f2=32 f3=11 f4=1 f5=1
  40fe01                          f8=254
  4811                            f9=17   <-- BEFORE f11!
  5017                            f10=23  <-- BEFORE f11!
  5801                            f11=1
  70 [seq]                        f14=seq
  7860 800104 880101              f15=96 f16=4 f17=1
  ba0107 Android                  f23="Android"
  d20110 [SN]                     f26=SN
  da0110 [SN]                     f27=SN  (repeated, confirmed)

Field order: 1, 2, 3, 4, 5, 8, 9, 10, 11, 14, 15, 16, 17, 23, 26, 27
"""

import json, os, struct, time, threading, sys, urllib.request
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
TEST_ID    = f"ANDROID_{int(_p[1])+13}_{SESSION_ID}"

SN        = "HR65ZA1AVH7J0027"
MQTT_HOST = "mqtt.ecoflow.com"
MQTT_PORT = 8883

SET_TOPIC  = f"/app/{SESSION_ID}/{SN}/thing/property/set"
TELE_TOPIC = f"/app/device/property/{SN}"
GET_TOPIC  = f"/app/{SESSION_ID}/{SN}/thing/property/get"
GET_MSG    = json.dumps({"from": "HomeAssistant", "id": "999", "version": "1.1",
                         "moduleType": 0, "operateType": "latestQuotas", "params": {}})

# ── Protobuf primitives ───────────────────────────────────────────────────────
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


def build_inner(field1_content, f9, f10, seq, sn=SN):
    """
    Build inner message with CORRECT field order matching the EcoFlow app.
    Order: f1, f2, f3, f4, f5, f8, f9, f10, f11, f14, f15, f16, f17, f23, f26, f27
    """
    return (
        pb(1,  2, field1_content)  +   # sub-msg (pdata wrapper or heartbeat)
        pb(2,  0, 32)              +   # cmdSet=32
        pb(3,  0, 11)              +   # cmdId=11
        pb(4,  0, 1)               +   # f4=1
        pb(5,  0, 1)               +   # f5=1
        pb(8,  0, 254)             +   # cf=254
        pb(9,  0, f9)              +   # f9 (17=cmd, 19=heartbeat)
        pb(10, 0, f10)             +   # f10 (pdata_len+3=cmd, 4=heartbeat)
        pb(11, 0, 1)               +   # f11=1
        pb(14, 0, seq)             +   # seq (keep <128 for single byte)
        pb(15, 0, 96)              +   # f15=96
        pb(16, 0, 4)               +   # f16=4
        pb(17, 0, 1)               +   # f17=1
        pb(23, 2, b"Android")      +   # device type
        pb(26, 2, sn.encode())     +   # SN
        pb(27, 2, sn.encode())         # SN (repeated, from captures)
    )


def build_heartbeat(seq, sn=SN):
    """83-byte heartbeat. Seq must be < 128."""
    # Reference inner starts: 0a04 0a02 8101
    # pb(1,2,inner) wraps it so f1_content must be the 4-byte block: 0a02 8101
    f1_content = pb(1, 2, b'\x81\x01')   # = 0a 02 81 01  (4 bytes)
    inner = build_inner(f1_content, f9=19, f10=4, seq=seq, sn=sn)
    return pb(1, 2, inner)


def build_charge_on_pdata(watts=500.0):
    sub = pb(1, 0, 4) + pb(2, 5, watts)
    return (
        pb(2,  0, 1)     +
        pb(5,  0, 1)     +
        pb(6,  0, 127)   +
        pb(7,  0, 12345678) +
        pb(10, 2, sub)
    )


def build_charge_off_pdata(watts=500.0):
    sub = pb(1, 0, 4) + pb(2, 5, watts)
    return (
        pb(1,  0, 0xFFFFFFFF) +
        pb(2,  0, 1)          +
        pb(3,  0, 1)          +
        pb(5,  0, 1)          +
        pb(6,  0, 127)        +
        pb(7,  0, 12345679)   +
        pb(10, 2, sub)
    )


def build_charge_cmd(pdata, seq, sn=SN):
    """102-byte (ON) or 110-byte (OFF) command. Seq must be < 128."""
    f127  = pb(127, 2, pdata)
    inner = build_inner(f127, f9=17, f10=len(pdata)+3, seq=seq, sn=sn)
    return pb(1, 2, inner)


# ── Verify correct field order ────────────────────────────────────────────────
_hb     = build_heartbeat(seq=74)           # seq=74=0x4a to match ref
_on     = build_charge_cmd(build_charge_on_pdata(80.0), seq=76)   # match ref

REF_HB  = bytes.fromhex("0a510a040a0281011020180b2001280140fe01481350045801704a7860800104880101ba0107416e64726f6964d20110485236355a4131415648374a30303237")
REF_ON  = bytes.fromhex("0a640a17fa071410012801307f38b481a80952070804150000a0421020180b2001280140fe01481150175801704c7860800104880101ba0107416e64726f6964d20110485236355a4131415648374a")

print("=" * 70)
print("FIELD ORDER VERIFICATION")
print(f"  HB  size: {len(_hb)}b  (expected 83)")
print(f"  ON  size: {len(_on)}b  (expected 102+)")
print()

# Compare heartbeat (excluding mystery tail bytes at end of ref = shorter ref)
n = len(REF_HB)
our_hb_pre = _hb.hex()[:n*2]
ref_hb_hex = REF_HB.hex()
diff_positions = [i for i in range(0, n*2, 2) if our_hb_pre[i:i+2] != ref_hb_hex[i:i+2]]
print(f"HB comparison (first {n} bytes):")
print(f"  REF: {ref_hb_hex}")
print(f"  OUR: {our_hb_pre}")
if not diff_positions:
    print("  MATCH: All bytes identical (excluding mystery tail)")
else:
    print(f"  DIFF at byte positions: {[p//2 for p in diff_positions]}")
    for p in diff_positions:
        bi = p // 2
        print(f"    byte[{bi}]: ref={ref_hb_hex[p:p+2]} our={our_hb_pre[p:p+2]}")

print()
n2 = len(REF_ON)
our_on_pre = _on.hex()[:n2*2]
ref_on_hex = REF_ON.hex()
diff2 = [i for i in range(0, n2*2, 2) if our_on_pre[i:i+2] != ref_on_hex[i:i+2]]
print(f"ON  comparison (first {n2} bytes, excluding f7/watts diffs):")
# Known diffs: bytes 14-17 (field7) and 25-26 (watts float)
structural_diffs = [p for p in diff2 if p//2 not in range(14,18) and p//2 not in range(25,27)]
if not structural_diffs:
    print(f"  Only expected diffs (field7/watts). Structural fields MATCH.")
else:
    print(f"  UNEXPECTED structural diffs at: {[p//2 for p in structural_diffs]}")
    for p in structural_diffs:
        bi = p // 2
        print(f"    byte[{bi}]: ref={ref_on_hex[p:p+2]} our={our_on_pre[p:p+2]}")
print()

# Final size check
pdata_on  = build_charge_on_pdata(500.0)
pdata_off = build_charge_off_pdata(500.0)
cmd_on    = build_charge_cmd(pdata_on,  seq=1)
cmd_off   = build_charge_cmd(pdata_off, seq=2)
hb_test   = build_heartbeat(seq=1)
print(f"Final sizes (with field7=12345678, 500W, seq=1/2):")
print(f"  HB  = {len(hb_test)}b  (expected 83)")
print(f"  ON  = {len(cmd_on)}b  (expected 102)")
print(f"  OFF = {len(cmd_off)}b  (expected 110)")
print()
print(f"ON  hex: {cmd_on.hex()}")
print(f"OFF hex: {cmd_off.hex()}")

# ── REST mode switch ──────────────────────────────────────────────────────────
def set_mode(target_mode):
    if not REST_JWT:
        print("  [REST] No JWT — skipping")
        return {}
    url  = "https://api-a.ecoflow.com/tou-service/goe/ai-mode/notify-mode-changed"
    hdrs = {"Authorization": f"Bearer {REST_JWT}", "Content-Type": "application/json",
            "lang": "en-us", "countryCode": "US", "platform": "android",
            "version": "6.11.0.1731", "User-Agent": "okhttp/4.11.0", "X-Appid": "-1"}
    body = json.dumps({"sn": SN, "systemNo": "", "targetMode": target_mode}).encode()
    req  = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

# ── Telemetry decoder ─────────────────────────────────────────────────────────
def dv(d, p):
    r, s = 0, 0
    while p < len(d):
        b = d[p]; p += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80): break
        s += 7
    return r, p

def decode_telemetry(payload):
    fields = {}
    try:
        tag, pos = dv(payload, 0)
        if (tag >> 3) != 1 or (tag & 7) != 2: return fields
        ln, pos = dv(payload, pos)
        inner = payload[pos:pos+ln]
        ipos = 0
        while ipos < len(inner):
            try:
                itag, ipos = dv(inner, ipos)
                ifn = itag >> 3; iwt = itag & 7
                if iwt == 0:
                    iv, ipos = dv(inner, ipos)
                    if ifn == 1544: fields["home_w"] = iv
                elif iwt == 2:
                    iln, ipos = dv(inner, ipos)
                    chunk = inner[ipos:ipos+iln]; ipos += iln
                    if ifn == 1:
                        pp = 0
                        while pp < len(chunk):
                            try:
                                ptag, pp = dv(chunk, pp)
                                pfn = ptag >> 3; pwt = ptag & 7
                                if pwt == 0:
                                    pv, pp = dv(chunk, pp)
                                elif pwt == 2:
                                    pln, pp = dv(chunk, pp)
                                    pc = chunk[pp:pp+pln]; pp += pln
                                    if pfn == 518 and pln == 4:
                                        fields["bat_w"] = struct.unpack("<f", pc)[0]
                                    elif pfn == 1009:
                                        sp = 0
                                        while sp < len(pc):
                                            st, sp = dv(pc, sp)
                                            sf = st >> 3; sw = st & 7
                                            if sw == 0:
                                                sv, sp = dv(pc, sp)
                                                if sf == 5: fields["soc"] = sv
                                                if sf == 4:
                                                    fields["mode"] = {1:"BACKUP",2:"SELF-PWRD"}.get(sv,sv)
                                            else: break
                                elif pwt == 5:
                                    pv = struct.unpack_from("<f", chunk, pp)[0]; pp += 4
                                    if pfn == 515: fields["grid_w"] = pv
                                else: break
                            except: break
                elif iwt == 5:
                    iv = struct.unpack_from("<f", inner, ipos)[0]; ipos += 4
                    if ifn == 515: fields["grid_w"] = iv
                else: break
            except: break
    except: pass
    return fields

# ── MQTT ──────────────────────────────────────────────────────────────────────
connected   = threading.Event()
tele_log    = []
tele_lock   = threading.Lock()
stop_hb     = threading.Event()
mqtt_cli    = [None]

def on_connect(c, u, f, rc):
    if rc == 0:
        print(f"  MQTT connected as {TEST_ID}")
        c.subscribe(TELE_TOPIC, qos=0)
        connected.set()
    else:
        print(f"  MQTT connect FAILED rc={rc}")

def on_message(c, u, msg):
    if len(msg.payload) < 20: return
    flds = decode_telemetry(msg.payload)
    if not flds: return
    ts = time.strftime("%H:%M:%S")
    parts = []
    if "bat_w"  in flds: parts.append(f"Battery={flds['bat_w']:+.1f}W")
    if "home_w" in flds: parts.append(f"Home={flds['home_w']}W")
    if "grid_w" in flds: parts.append(f"Grid={flds['grid_w']:.0f}W")
    if "soc"    in flds: parts.append(f"SOC={flds['soc']}%")
    if "mode"   in flds: parts.append(f"Mode={flds['mode']}")
    if parts:
        line = " | ".join(parts)
        with tele_lock: tele_log.append((ts, line))
        print(f"  [{ts}] {line}")

def heartbeat_loop():
    cli = mqtt_cli[0]
    hb_seq = [1]
    while not stop_hb.is_set():
        s = hb_seq[0] % 127 + 1
        hb_seq[0] += 1
        cli.publish(SET_TOPIC, build_heartbeat(seq=s), qos=0)
        stop_hb.wait(timeout=1.0)

print()
confirm = input("Press ENTER to run MQTT test (or Ctrl+C to abort): ")

try:
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=TEST_ID, protocol=mqtt.MQTTv311)
except AttributeError:
    cli = mqtt.Client(client_id=TEST_ID, protocol=mqtt.MQTTv311)
cli.username_pw_set(MQTT_USER, MQTT_PASS)
cli.tls_set()
cli.on_connect = on_connect
cli.on_message = on_message
mqtt_cli[0] = cli

print(f"\nConnecting as {TEST_ID}...")
cli.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
cli.loop_start()
if not connected.wait(timeout=10):
    print("  MQTT timeout — aborting")
    sys.exit(1)

# Step 1: REST backup mode
print("\n[1] Setting REST Backup mode (-1)...")
r = set_mode(-1)
print(f"    {r.get('code','?')} {r.get('message','')}")
time.sleep(2)

# Step 2: Baseline
print("\n[2] Baseline telemetry...")
cli.publish(GET_TOPIC, GET_MSG, qos=1)
time.sleep(5)

# Step 3: 5s heartbeats
print("\n[3] Sending 5s of heartbeats (83b each at 1/sec)...")
hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
hb_thread.start()
time.sleep(5)

# Step 4: Charge ON
print("\n[4] CHARGE ON command (seq=1, 500W, 102b)...")
cmd_on_final  = build_charge_cmd(build_charge_on_pdata(500.0), seq=1)
cmd_off_final = build_charge_cmd(build_charge_off_pdata(500.0), seq=2)
rc, mid = cli.publish(SET_TOPIC, cmd_on_final, qos=1)
print(f"    Published rc={rc} mid={mid} len={len(cmd_on_final)}")
print(f"    HEX: {cmd_on_final.hex()}")
print("    Watching 25s (battery should go POSITIVE if charging starts)...")
cli.publish(GET_TOPIC, GET_MSG, qos=1)
time.sleep(25)

# Step 5: Charge OFF
print("\n[5] CHARGE OFF command (seq=2, 500W, 110b)...")
rc, mid = cli.publish(SET_TOPIC, cmd_off_final, qos=1)
print(f"    Published rc={rc} mid={mid} len={len(cmd_off_final)}")
cli.publish(GET_TOPIC, GET_MSG, qos=1)
time.sleep(15)

stop_hb.set()
cli.loop_stop()

print("\n" + "=" * 70)
print("TEST COMPLETE - Telemetry during test:")
with tele_lock:
    for ts, line in tele_log:
        print(f"  [{ts}] {line}")
print("=" * 70)