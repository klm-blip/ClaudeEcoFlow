#!/usr/bin/env python3
"""
EcoFlow charge control v10 - Target DPUX directly via DPU SET TOPIC

Key findings from v7-v9:
- ESG ConfigWrite (cf=254/ci=17/dest=11/ver=3): accepts fields 33, 7, 34 with config_ok=1
  BUT has NO effect on charging (v7 confirmed with real telemetry: 5050W throughout)
- cmdFunc=12 (ProtoPushAndSet) doesn't work on ESG
- cmdFunc=2/ci=87 doesn't get reply when targeting ESG
- Field 518 (batt_w) absent from ESG packets when battery is idle
- Current state: battery idle, house drawing 739W from grid

NEW APPROACH: Target DPUX (P101ZA1A9HA70164) directly via its own SET topic
/app/{USER_ID}/{SN_DPU}/thing/property/set

Tests:
A) DPUX ConfigWrite (cf=254/ci=17/dest=2/ver=3) + cms_max_chg_soc=80 (field 33)
   - Previously tried with field 33=0 (no effect). Now trying NON-ZERO value.
B) DPUX native cmdFunc=2/cmdId=87 + chgMaxSoc=80 (field 1)
   - DPUX ioBroker data: chgMaxSoc: { msg: { dest: 2, cmdFunc: 2, cmdId: 87, dataLen: 2 } }
   - This is the DPUX's NATIVE chgMaxSoc command. Haven't tried this on DPU topic yet!
C) DPUX ConfigWrite + cms_max_chg_soc=1 (near-zero)
D) DPUX ConfigWrite + cms_max_chg_soc=50
E) Try different versions for cmdFunc=2/cmdId=87 (version=3 and version=4)

Note: Charging is currently idle. Commands will be sent anyway to get ACKs.
When charging is next active (user starts from app), re-run this script to verify effect.
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

# ESG topics
SET_TOPIC_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set'
DATA_TOPIC_ESG = f'/app/device/property/{SN_ESG}'
SET_REPLY_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply'
GET_TOPIC_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/get'

# DPU topics (NEW - targeting DPUX directly)
SET_TOPIC_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/set'
DATA_TOPIC_DPU = f'/app/device/property/{SN_DPU}'
SET_REPLY_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/set_reply'
GET_TOPIC_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/get'

# Protobuf helpers (v7-confirmed)
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

def build_cmd(pdata_bytes, cmd_func=254, cmd_id=17, dest=2, version=3, src=32):
    """Build command packet."""
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    header = (
        pb_bytes(1, pdata_bytes) + pb_varint(2, src) + pb_varint(3, dest) +
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

# State
state = {
    'batt_w': None, 'soc': None, 'grid_w': None, 'home_w': None,
    'msg_count': 0, 'esg_replies': [], 'dpu_replies': [],
    'charging': False
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

        if topic == SET_REPLY_ESG:
            ack = decode_ack(pdata_bytes)
            state['esg_replies'].append({'cf': cf, 'ci': ci, 'src': src, 'ver': ver, 'ack': ack})
            print(f"\n[ESG reply] cf={cf}/ci={ci}/src={src}/ver={ver} ACK={ack}")

        elif topic == SET_REPLY_DPU:
            ack = decode_ack(pdata_bytes)
            state['dpu_replies'].append({'cf': cf, 'ci': ci, 'src': src, 'ver': ver, 'ack': ack})
            print(f"\n{'!'*70}")
            print(f"[DPU REPLY] cf={cf}/ci={ci}/src={src}/ver={ver}")
            print(f"  ACK: {ack}")
            print(f"{'!'*70}")

        elif topic == DATA_TOPIC_ESG:
            if isinstance(pdata_bytes, bytes) and len(pdata_bytes) > 0:
                pdata = parse_fields(pdata_bytes)
                # f518 = batt watts (wire type 2, 4-byte float)
                if 518 in pdata and isinstance(pdata[518], bytes) and len(pdata[518]) == 4:
                    bw = struct.unpack('<f', pdata[518])[0]
                    state['batt_w'] = bw
                    state['charging'] = bw > 100
                # f515 = grid watts
                if 515 in pdata and isinstance(pdata[515], bytes) and len(pdata[515]) == 4:
                    state['grid_w'] = struct.unpack('<f', pdata[515])[0]
                # f1544 = home load
                if 1544 in pdata:
                    v = pdata[1544]
                    state['home_w'] = v if isinstance(v, int) else (struct.unpack('<f', v)[0] if isinstance(v, bytes) and len(v)==4 else None)
                # f1009 = SOC sub-message
                if 1009 in pdata and isinstance(pdata[1009], bytes):
                    sub = parse_fields(pdata[1009])
                    if 5 in sub and isinstance(sub[5], bytes) and len(sub[5]) == 4:
                        state['soc'] = struct.unpack('<f', sub[5])[0]
            bw_str = f"{state['batt_w']:.0f}W" if state['batt_w'] else "idle"
            print(f"  [ESG] grid={state['grid_w']:.0f}W home={state['home_w']}W batt={bw_str} soc={state['soc']}")

        elif topic == DATA_TOPIC_DPU:
            if isinstance(pdata_bytes, bytes) and len(pdata_bytes) > 0:
                pdata = parse_fields(pdata_bytes)
                if 518 in pdata and isinstance(pdata[518], bytes) and len(pdata[518]) == 4:
                    bw = struct.unpack('<f', pdata[518])[0]
                    print(f"  [DPU] batt_w={bw:.0f}W (f518)")
                elif 262 in pdata and isinstance(pdata[262], bytes) and len(pdata[262]) == 4:
                    soc = struct.unpack('<f', pdata[262])[0]
                    print(f"  [DPU] cms_batt_soc={soc:.1f}% (f262)")
    except Exception as e:
        print(f"  [err: {e}]")

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected")
        client.subscribe([
            (DATA_TOPIC_ESG, 0), (DATA_TOPIC_DPU, 0),
            (SET_REPLY_ESG, 0), (SET_REPLY_DPU, 0)
        ])
        print("Subscribed")
        # Trigger telemetry from both
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

print("Waiting 12s for baseline telemetry...")
time.sleep(12)

batt_now = state['batt_w']
soc_now = state['soc']
grid_now = state['grid_w']
home_now = state['home_w']
print(f"\nBaseline: batt_w={batt_now}, soc={soc_now}, grid={grid_now}, home={home_now}")
print(f"Charging: {state['charging']}")
if not state['charging']:
    print("  NOTE: Battery not currently charging. Commands will be sent anyway to verify ACKs.")
    print("  Run again with charging active to verify behavior change.")

ts = int(time.time())

# ── TEST A: DPUX ConfigWrite via DPU SET TOPIC ──────────────────────
print(f"\n{'='*70}")
print("TEST A: DPUX ConfigWrite to DPU SET TOPIC - cms_max_chg_soc=80")
print(f"  Topic: {SET_TOPIC_DPU}")
print(f"  cf=254/ci=17/dest=2/ver=3, field 6=ts + field 33=80")
pdata_a = pb_varint(6, ts) + pb_varint(33, 80)
print(f"  pdata: {pdata_a.hex()}")
dpu_replies_before = len(state['dpu_replies'])
rc_a = client.publish(SET_TOPIC_DPU, build_cmd(pdata_a, cmd_func=254, cmd_id=17, dest=2, version=3), qos=1)
print(f"  Published rc={rc_a.rc}")
time.sleep(8)
new_dpu = state['dpu_replies'][dpu_replies_before:]
print(f"  DPU replies: {[r['ack'] for r in new_dpu]}")
print(f"  ESG replies: {[r['ack'] for r in state['esg_replies']]}")

ts = int(time.time())

# ── TEST B: DPUX native cmdFunc=2/cmdId=87 via DPU SET TOPIC ────────
print(f"\n{'='*70}")
print("TEST B: DPUX native chgMaxSoc via DPU SET TOPIC - cmdFunc=2/cmdId=87")
print(f"  Topic: {SET_TOPIC_DPU}")
print(f"  cf=2/ci=87/dest=2/ver=3, field 1=80 (chgMaxSoc=80%)")
pdata_b = pb_varint(1, 80)
print(f"  pdata: {pdata_b.hex()}")
dpu_replies_before = len(state['dpu_replies'])
rc_b = client.publish(SET_TOPIC_DPU, build_cmd(pdata_b, cmd_func=2, cmd_id=87, dest=2, version=3), qos=1)
print(f"  Published rc={rc_b.rc}")
time.sleep(8)
new_dpu = state['dpu_replies'][dpu_replies_before:]
print(f"  DPU replies: {[r['ack'] for r in new_dpu]}")

ts = int(time.time())

# ── TEST C: DPUX native chgMaxSoc version=4 ─────────────────────────
print(f"\n{'='*70}")
print("TEST C: Same as B but version=4 (DPUX telemetry shows version=3, trying 4)")
pdata_c = pb_varint(1, 80)
dpu_replies_before = len(state['dpu_replies'])
rc_c = client.publish(SET_TOPIC_DPU, build_cmd(pdata_c, cmd_func=2, cmd_id=87, dest=2, version=4), qos=1)
print(f"  Published rc={rc_c.rc}")
time.sleep(8)
new_dpu = state['dpu_replies'][dpu_replies_before:]
print(f"  DPU replies: {[r['ack'] for r in new_dpu]}")

ts = int(time.time())

# ── TEST D: DPUX ConfigWrite cms_max_chg_soc=1 (near-zero) ──────────
print(f"\n{'='*70}")
print("TEST D: DPUX ConfigWrite cms_max_chg_soc=1 (near-zero, definitely stops at any SOC)")
pdata_d = pb_varint(6, ts) + pb_varint(33, 1)
dpu_replies_before = len(state['dpu_replies'])
rc_d = client.publish(SET_TOPIC_DPU, build_cmd(pdata_d, cmd_func=254, cmd_id=17, dest=2, version=3), qos=1)
print(f"  Published rc={rc_d.rc}")
time.sleep(8)
new_dpu = state['dpu_replies'][dpu_replies_before:]
print(f"  DPU replies: {[r['ack'] for r in new_dpu]}")

ts = int(time.time())

# ── TEST E: DPUX cmdFunc=2/cmdId=87 to ESG SET TOPIC ─────────────────
# Different from v8: previously targeted ESG but used ESG dest. Now use dest=2 but ESG topic
print(f"\n{'='*70}")
print("TEST E: cmdFunc=2/cmdId=87/dest=2 to ESG SET TOPIC (routing through ESG)")
pdata_e = pb_varint(1, 80)
esg_replies_before = len(state['esg_replies'])
rc_e = client.publish(SET_TOPIC_ESG, build_cmd(pdata_e, cmd_func=2, cmd_id=87, dest=2, version=3), qos=1)
print(f"  Published rc={rc_e.rc}")
time.sleep(8)
new_esg = state['esg_replies'][esg_replies_before:]
print(f"  ESG replies: {[r['ack'] for r in new_esg]}")

ts = int(time.time())

# ── TEST F: RESTORE DPUX ─────────────────────────────────────────────
print(f"\n{'='*70}")
print("TEST F: RESTORE - DPUX ConfigWrite cms_max_chg_soc=100")
pdata_f = pb_varint(6, ts) + pb_varint(33, 100)
client.publish(SET_TOPIC_DPU, build_cmd(pdata_f, cmd_func=254, cmd_id=17, dest=2, version=3), qos=1)
time.sleep(8)

# Also restore ESG
ts = int(time.time())
pdata_restore_esg = pb_varint(6, ts) + pb_varint(33, 100) + pb_varint(7, 7200)
client.publish(SET_TOPIC_ESG, build_cmd(pdata_restore_esg, cmd_func=254, cmd_id=17, dest=11, version=3), qos=1)
time.sleep(5)
print("  Restore sent")

# ── FINAL SUMMARY ─────────────────────────────────────────────────────
print("\n" + "="*70)
print("FINAL SUMMARY")
print("="*70)
print(f"  Battery state: batt_w={state['batt_w']}, soc={state['soc']}, grid={state['grid_w']}")
print(f"  Charging: {state['charging']}")
print(f"  Total msgs: {state['msg_count']}")
print(f"\n  ESG replies ({len(state['esg_replies'])} total):")
for r in state['esg_replies']:
    print(f"    cf={r['cf']}/ci={r['ci']}/src={r['src']}: {r['ack']}")
print(f"\n  DPU replies ({len(state['dpu_replies'])} total):")
for r in state['dpu_replies']:
    print(f"    cf={r['cf']}/ci={r['ci']}/src={r['src']}: {r['ack']}")

if state['dpu_replies']:
    print("\n  *** DPU REPLIED! This is new - DPUX accepts commands on its own SET topic ***")
else:
    print("\n  *** DPU silent to all commands on DPU SET TOPIC ***")

client.loop_stop()
client.disconnect()
