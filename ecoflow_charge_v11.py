#!/usr/bin/env python3
"""
EcoFlow charge control v11 - Test DPU cms_max_chg_soc with ACTIVE charging

BREAKTHROUGH from v10:
- DPU SET TOPIC (/app/{USER_ID}/{SN_DPU}/thing/property/set) WORKS!
- DPU ConfigWrite (cf=254/ci=17/dest=2/ver=3) sends TWO ACKs per command:
  1. ACK for cfgUtcTime (field 6): {'action_id': 6, 'config_ok': 1, 'field_6': timestamp}
  2. ACK for target field: {'action_id': 33, 'config_ok': 1, 'field_33': value_echoed}
- DPU ECHOES BACK the stored value! field_33=80 in the ACK = confirmed stored 80%
- ESG ConfigWrite (same format, ESG topic) does NOT echo back values, and does NOT affect charging
- cmdFunc=2/cmdId=87 doesn't reply from DPU (likely different topic or pdata needed)

Current state: SOC=85%, battery IDLE

THIS SCRIPT:
- Waits up to 5 minutes for charging to start (watches for batt_w > 500W from ESG)
- Once charging detected at baseline >500W:
  PHASE 1: Set cms_max_chg_soc = current_soc - 3 (just below current SOC)
           Watch 45s - if charging stops, DPU limit IS enforced!
  PHASE 2: If still charging, try cms_max_chg_soc = 1 (extreme)
           Watch 45s
  RESTORE: Set cms_max_chg_soc = 100

HOW TO USE:
1. Start this script
2. Start charging from the EcoFlow app (charge to 100%, or manual charge ~5kW)
3. Script will auto-detect charging and run tests

Note: If charging does NOT start within 5 minutes, script reports current DPU response
and exits gracefully.
"""
import ssl, json, time, struct
import paho.mqtt.client as mqtt

creds = {}
with open('ecoflow_credentials.txt') as f:
    for line in f:
        if '=' in line:
            k, v = line.strip().split('=', 1)
            creds[k.strip()] = v.strip()

CLIENT_ID  = creds['CLIENT_ID']
MQTT_USER  = creds['MQTT_USER']
MQTT_PASS  = creds['MQTT_PASS']
parts      = CLIENT_ID.split('_', 2)
USER_ID    = parts[2] if len(parts) >= 3 else parts[-1]

SN_ESG = 'HR65ZA1AVH7J0027'
SN_DPU = 'P101ZA1A9HA70164'

SET_TOPIC_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/set'
SET_TOPIC_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set'
DATA_TOPIC_ESG = f'/app/device/property/{SN_ESG}'
DATA_TOPIC_DPU = f'/app/device/property/{SN_DPU}'
SET_REPLY_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/set_reply'
SET_REPLY_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply'
GET_TOPIC_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/get'
GET_TOPIC_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/get'

# Protobuf helpers
def pb_varint(field, value):
    tag = (field << 3) | 0
    result = b''
    v = tag
    while v > 0x7F: result += bytes([0x80|(v&0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    v = value
    while v > 0x7F: result += bytes([0x80|(v&0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    return result

def pb_bytes(field, data):
    tag = (field << 3) | 2
    result = b''
    v = tag
    while v > 0x7F: result += bytes([0x80|(v&0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    length = len(data)
    v = length
    while v > 0x7F: result += bytes([0x80|(v&0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    return result + data

def pb_string(field, s):
    return pb_bytes(field, s.encode('utf-8'))

def build_dpu_cmd(pdata_bytes, cmd_func=254, cmd_id=17, dest=2, version=3):
    """Build ConfigWrite command for DPUX via DPU SET TOPIC."""
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    header = (
        pb_bytes(1, pdata_bytes) + pb_varint(2, 32) + pb_varint(3, dest) +
        pb_varint(4, 1) + pb_varint(5, 1) + pb_varint(8, cmd_func) + pb_varint(9, cmd_id) +
        pb_varint(10, len(pdata_bytes)) + pb_varint(11, 1) + pb_varint(14, seq) +
        pb_varint(16, version) + pb_varint(17, 1) + pb_string(23, 'Android')
    )
    return pb_bytes(1, header)

def parse_fields(payload):
    fields = {}; i = 0
    while i < len(payload):
        tag = 0; shift = 0
        try:
            while i < len(payload):
                b = payload[i]; i += 1
                tag |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
        except: break
        fn = tag >> 3; wt = tag & 7
        if wt == 0:
            val = 0; shift = 0
            while i < len(payload):
                b = payload[i]; i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            fields[fn] = val
        elif wt == 2:
            length = 0; shift = 0
            while i < len(payload):
                b = payload[i]; i += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            fields[fn] = payload[i:i+length]; i += length
        elif wt == 5:
            fields[fn] = payload[i:i+4]; i += 4
        else: break
    return fields

def decode_ack(pdata_bytes):
    if not pdata_bytes: return {}
    p = parse_fields(pdata_bytes)
    result = {}
    if 1 in p: result['action_id'] = p[1]
    if 2 in p: result['config_ok'] = p[2]
    for k, v in p.items():
        if k not in (1, 2):
            result[f'field_{k}'] = v if not isinstance(v, bytes) else v.hex()
    return result

def send_max_chg_soc(soc_val):
    """Send cms_max_chg_soc=soc_val to DPUX and return ACK."""
    ts = int(time.time())
    pdata = pb_varint(6, ts) + pb_varint(33, soc_val)
    acks_before = len(state['dpu_replies'])
    payload = build_dpu_cmd(pdata)
    client.publish(SET_TOPIC_DPU, payload, qos=1)
    # Wait up to 5s for ACK
    for _ in range(10):
        time.sleep(0.5)
        new_acks = state['dpu_replies'][acks_before:]
        if any(a['ack'].get('action_id') == 33 for a in new_acks):
            return [a['ack'] for a in new_acks]
    return [a['ack'] for a in state['dpu_replies'][acks_before:]]

# State
state = {
    'batt_w': None, 'soc': None, 'grid_w': None, 'home_w': None,
    'msg_count': 0, 'dpu_replies': [], 'esg_replies': [],
    'batt_readings': []
}

def on_message(client, userdata, msg):
    try:
        topic = msg.topic
        payload = msg.payload
        state['msg_count'] += 1

        outer = parse_fields(payload)
        inner_bytes = outer.get(1, b'')
        if not inner_bytes: return
        inner = parse_fields(inner_bytes)
        cf = inner.get(8); ci = inner.get(9); src = inner.get(2); ver = inner.get(16)
        pdata_bytes = inner.get(1, b'') if isinstance(inner.get(1), bytes) else b''

        if topic == SET_REPLY_DPU:
            ack = decode_ack(pdata_bytes)
            state['dpu_replies'].append({'cf': cf, 'ci': ci, 'src': src, 'ver': ver, 'ack': ack})
            print(f"  [DPU ACK] cf={cf}/ci={ci}/src={src}: {ack}")

        elif topic == SET_REPLY_ESG:
            ack = decode_ack(pdata_bytes)
            state['esg_replies'].append({'cf': cf, 'ci': ci, 'src': src, 'ver': ver, 'ack': ack})
            print(f"  [ESG ACK] cf={cf}/ci={ci}: {ack}")

        elif topic == DATA_TOPIC_ESG:
            if isinstance(pdata_bytes, bytes) and len(pdata_bytes) > 0:
                pdata = parse_fields(pdata_bytes)
                # f518 = batt watts (wire type 2, 4-byte float)
                if 518 in pdata and isinstance(pdata[518], bytes) and len(pdata[518]) == 4:
                    bw = struct.unpack('<f', pdata[518])[0]
                    state['batt_w'] = bw
                    state['batt_readings'].append((time.time(), bw))
                # f515 = grid watts
                if 515 in pdata and isinstance(pdata[515], bytes) and len(pdata[515]) == 4:
                    state['grid_w'] = struct.unpack('<f', pdata[515])[0]
                # f1544 = home load
                if 1544 in pdata:
                    v = pdata[1544]
                    state['home_w'] = v if isinstance(v, int) else None
                # f1009 = SOC sub-message
                if 1009 in pdata and isinstance(pdata[1009], bytes):
                    sub = parse_fields(pdata[1009])
                    if 5 in sub and isinstance(sub[5], bytes) and len(sub[5]) == 4:
                        state['soc'] = struct.unpack('<f', sub[5])[0]
            bw = state['batt_w']
            soc = state['soc']
            gw = state['grid_w']
            if bw is not None and bw > 100:
                print(f"  *** CHARGING: batt={bw:.0f}W grid={gw:.0f}W soc={soc:.1f}% ***")
            # Note: don't print every idle reading

    except Exception as e:
        print(f"  [err: {e}]")

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected")
        client.subscribe([
            (DATA_TOPIC_ESG, 0), (DATA_TOPIC_DPU, 0),
            (SET_REPLY_DPU, 0), (SET_REPLY_ESG, 0)
        ])
        print("Subscribed")
        get_payload = json.dumps({'from':'HomeAssistant','id':'99','version':'1.1','moduleType':0,'operateType':'latestQuotas','params':{}})
        client.publish(GET_TOPIC_ESG, get_payload, qos=1)
        client.publish(GET_TOPIC_DPU, get_payload, qos=1)
    else:
        print(f"Connect failed rc={rc}")

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
client.on_connect = on_connect
client.on_message = on_message
client.username_pw_set(MQTT_USER, MQTT_PASS)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
client.tls_set_context(ctx)
client.connect('mqtt.ecoflow.com', 8883, 60)
client.loop_start()

# ── INITIAL STATUS ─────────────────────────────────────────────────
print("="*70)
print("EcoFlow Charge Control v11")
print("="*70)
print("Waiting 10s for initial telemetry...")
time.sleep(10)

soc_now = state['soc']
batt_now = state['batt_w']
grid_now = state['grid_w']
print(f"\nInitial state: SOC={soc_now}, batt_w={batt_now}, grid={grid_now}")
print(f"Battery: {'CHARGING' if batt_now and batt_now > 100 else 'IDLE'}")

# ── WAIT FOR CHARGING ──────────────────────────────────────────────
print("\n" + "="*70)
print("WAITING FOR CHARGING TO START (up to 5 minutes)...")
print(">>> PLEASE START CHARGING FROM THE ECOFLOW APP NOW <<<")
print("="*70)

charging_detected = False
baseline_batt = None
baseline_soc = None
wait_start = time.time()
WAIT_TIMEOUT = 300  # 5 minutes

while time.time() - wait_start < WAIT_TIMEOUT:
    time.sleep(2)
    bw = state['batt_w']
    soc = state['soc']
    elapsed = int(time.time() - wait_start)

    if bw is not None and bw > 500:
        charging_detected = True
        baseline_batt = bw
        baseline_soc = soc
        print(f"\n*** CHARGING DETECTED at t+{elapsed}s: batt_w={bw:.0f}W, SOC={soc}% ***")
        break

    if elapsed % 30 == 0 and elapsed > 0:
        print(f"  [{elapsed}s] Still waiting... batt_w={bw} soc={soc}")

if not charging_detected:
    print(f"\nNo charging detected in {WAIT_TIMEOUT}s. Proceeding with DPU command tests anyway.")
    # Get a fresh status
    time.sleep(3)
    print(f"Current: batt_w={state['batt_w']}, soc={state['soc']}, grid={state['grid_w']}")

    # Verify DPU still responds
    print("\nVerifying DPU command response...")
    acks = send_max_chg_soc(100)
    print(f"DPU response to cms_max_chg_soc=100: {acks}")

    print("\n--- RESULTS ---")
    print("Battery was not charging during this session.")
    print("Cannot confirm whether cms_max_chg_soc controls charging behavior.")
    print("\nTo complete the test:")
    print("1. Start charging from the EcoFlow app (charge to 100%)")
    print("2. Run this script again")
    print("\nDPU ConfigWrite IS confirmed working (v10):")
    print("  - DPU echoes back stored values")
    print("  - field_33 (cms_max_chg_soc) stored correctly")
    client.loop_stop()
    client.disconnect()
    exit(0)

# ── CHARGING IS ACTIVE - RUN TESTS ────────────────────────────────
print(f"\nBaseline: batt_w={baseline_batt:.0f}W, SOC={baseline_soc}%")
# Let charging stabilize for 10s
print("Letting charging stabilize 10s...")
time.sleep(10)
stable_batt = state['batt_w']
stable_soc = state['soc']
print(f"Stable baseline: batt_w={stable_batt:.0f}W, SOC={stable_soc}%")

# ── PHASE 1: cms_max_chg_soc = SOC - 3 ───────────────────────────
if stable_soc:
    target_soc = max(1, int(stable_soc) - 3)
else:
    target_soc = 80

print(f"\n{'='*70}")
print(f"PHASE 1: Set cms_max_chg_soc={target_soc} (current SOC={stable_soc}%)")
print(f"  If DPU enforces this, charging should STOP (current SOC > max allowed)")
acks_p1 = send_max_chg_soc(target_soc)
print(f"  DPU ACKs: {acks_p1}")

print(f"  Watching battery for 45s...")
p1_readings = []
for t in range(45):
    time.sleep(1)
    bw = state['batt_w']
    soc = state['soc']
    if bw is not None:
        p1_readings.append(bw)
    if (t+1) % 10 == 0:
        print(f"  [{t+1}s] batt_w={bw:.0f}W soc={soc}" if bw else f"  [{t+1}s] batt_w=None")

p1_end = state['batt_w']
p1_effect = "CHARGING STOPPED" if (p1_end is None or (p1_end < stable_batt * 0.3)) else \
            "REDUCED" if (p1_end and p1_end < stable_batt * 0.7) else "NO CHANGE"
print(f"\nPhase 1 result: {p1_effect}")
print(f"  Start={stable_batt:.0f}W -> End={p1_end:.0f}W" if p1_end else f"  Start={stable_batt:.0f}W -> End=None")
if p1_readings:
    print(f"  Min during test: {min(p1_readings):.0f}W, Max: {max(p1_readings):.0f}W")

# ── PHASE 2: If no change, try cms_max_chg_soc = 1 ───────────────
if p1_effect == "NO CHANGE" and (p1_end and p1_end > stable_batt * 0.7):
    print(f"\n{'='*70}")
    print("PHASE 2: cms_max_chg_soc=1 (absolute minimum)")
    acks_p2 = send_max_chg_soc(1)
    print(f"  DPU ACKs: {acks_p2}")

    print(f"  Watching battery for 45s...")
    p2_readings = []
    for t in range(45):
        time.sleep(1)
        bw = state['batt_w']
        if bw is not None:
            p2_readings.append(bw)
        if (t+1) % 10 == 0:
            print(f"  [{t+1}s] batt_w={bw:.0f}W" if bw else f"  [{t+1}s] batt_w=None")

    p2_end = state['batt_w']
    p2_effect = "CHARGING STOPPED" if (p2_end is None or (p2_end is not None and p2_end < stable_batt * 0.3)) else \
                "REDUCED" if (p2_end and p2_end < stable_batt * 0.7) else "NO CHANGE"
    print(f"\nPhase 2 result: {p2_effect}")
    if p2_readings:
        print(f"  Min during test: {min(p2_readings):.0f}W, Max: {max(p2_readings):.0f}W")
else:
    p2_effect = "NOT TESTED"

# ── RESTORE ────────────────────────────────────────────────────────
print(f"\n{'='*70}")
print("RESTORE: cms_max_chg_soc=100")
acks_r = send_max_chg_soc(100)
print(f"  DPU ACKs: {acks_r}")
time.sleep(10)
final_batt = state['batt_w']
final_soc = state['soc']
print(f"  Post-restore: batt_w={final_batt}, soc={final_soc}")

# ── FINAL SUMMARY ─────────────────────────────────────────────────
print("\n" + "="*70)
print("FINAL SUMMARY")
print("="*70)
print(f"  Baseline charging rate: {stable_batt:.0f}W (SOC={stable_soc}%)")
print(f"  Phase 1 (cms_max_chg_soc={target_soc}%): {p1_effect}")
if p2_effect != "NOT TESTED":
    print(f"  Phase 2 (cms_max_chg_soc=1%): {p2_effect}")
else:
    print(f"  Phase 2: not needed (Phase 1 worked or charging already stopped)")
print(f"  Post-restore charging: batt_w={final_batt}")
print(f"\n  DPU replies total: {len(state['dpu_replies'])}")
for r in state['dpu_replies']:
    print(f"    {r['ack']}")

client.loop_stop()
client.disconnect()
