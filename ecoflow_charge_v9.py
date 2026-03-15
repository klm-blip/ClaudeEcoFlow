#!/usr/bin/env python3
"""
EcoFlow charge control v9 - Fix telemetry + verify cms_max_chg_soc effect

v8 telemetry fix:
- extract_batt_w was wrong: field 518 is wire-type 2 (4-byte LEN bytes), not fixed32
- Use v7's confirmed parsing: pdata[518] = bytes(4) -> unpack as float

Key questions to answer:
1. Does cms_max_chg_soc=80 (field 33, below current ~83% SOC) actually stop charging?
   -> Wait 30s after sending, watch for batt_w drop
2. Does charge_watt_power=100 (field 7) throttle charging?
3. Do ci=20 state messages from ESG change after our SET commands?
   -> Check if f6 in field_129 sub-messages changes from 127 to our value

Also try:
- STEP A: cf=254/ci=20 targeting ESG (send ESG's own state-push format as a command)
  -> field_129 sub-message with f5=1, f6=80 (set max SOC via state-push channel)
- STEP B: Field 34 (cms_min_dsg_soc from DPUX schema) as a sanity-check
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

SET_TOPIC_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set'
DATA_TOPIC_ESG = f'/app/device/property/{SN_ESG}'
DATA_TOPIC_DPU = f'/app/device/property/{SN_DPU}'
SET_REPLY_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply'
SET_REPLY_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/set_reply'
GET_TOPIC_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/get'

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

def build_esg_cmd(pdata_bytes, cmd_func=254, cmd_id=17, dest=11, version=3, src=32):
    """Build command for ESG."""
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    header = (
        pb_bytes(1, pdata_bytes) + pb_varint(2, src) + pb_varint(3, dest) +
        pb_varint(4, 1) + pb_varint(5, 1) + pb_varint(8, cmd_func) + pb_varint(9, cmd_id) +
        pb_varint(10, len(pdata_bytes)) + pb_varint(11, 1) + pb_varint(14, seq) +
        pb_varint(16, version) + pb_varint(17, 1) + pb_string(23, 'Android')
    )
    return pb_bytes(1, header)

# v7-CONFIRMED proto parsing
def parse_fields(payload):
    """Parse protobuf fields. Returns dict of field_num -> value."""
    fields = {}
    i = 0
    while i < len(payload):
        if i >= len(payload): break
        tag = 0; shift = 0
        while i < len(payload):
            b = payload[i]; i += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80): break
            shift += 7
        field_num = tag >> 3
        wire_type = tag & 7
        if wire_type == 0:
            val = 0; shift = 0
            while i < len(payload):
                b = payload[i]; i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            fields[field_num] = val
        elif wire_type == 2:
            length = 0; shift = 0
            while i < len(payload):
                b = payload[i]; i += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            fields[field_num] = payload[i:i+length]
            i += length
        elif wire_type == 5:
            fields[field_num] = payload[i:i+4]
            i += 4
        else:
            break
    return fields

def decode_ack(pdata_bytes):
    """Decode ConfigWriteAck (ci=18) pdata."""
    if not pdata_bytes: return {}
    p = parse_fields(pdata_bytes)
    result = {}
    if 1 in p: result['action_id'] = p[1]
    if 2 in p: result['config_ok'] = p[2]
    for k, v in p.items():
        if k not in (1, 2):
            result[f'field_{k}'] = v
    return result

def decode_ci20(pdata_bytes):
    """Decode ci=20 (ESG state push) pdata - specifically field_129 sub-messages."""
    if not pdata_bytes: return {}
    p = parse_fields(pdata_bytes)
    result = {'raw_fields': list(p.keys())}
    if 129 in p and isinstance(p[129], bytes):
        # repeated sub-messages in field 129
        submsg_data = p[129]
        # Parse as repeated field 1 items
        items = []
        i = 0
        while i < len(submsg_data):
            tag = 0; shift = 0
            while i < len(submsg_data):
                b = submsg_data[i]; i += 1
                tag |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            fn = tag >> 3; wt = tag & 7
            if wt == 2:
                length = 0; shift = 0
                while i < len(submsg_data):
                    b = submsg_data[i]; i += 1
                    length |= (b & 0x7F) << shift
                    if not (b & 0x80): break
                    shift += 7
                raw = submsg_data[i:i+length]
                i += length
                if length > 0:
                    sub = parse_fields(raw)
                    item = {}
                    for k, v in sub.items():
                        if isinstance(v, int): item[f'f{k}'] = v
                        elif isinstance(v, bytes):
                            if len(v) == 4:
                                item[f'f{k}_float'] = struct.unpack('<f', v)[0]
                            else:
                                sub2 = parse_fields(v)
                                for k2, v2 in sub2.items():
                                    if isinstance(v2, int): item[f'f{k}.f{k2}'] = v2
                                    elif isinstance(v2, bytes) and len(v2) == 4:
                                        item[f'f{k}.f{k2}_float'] = struct.unpack('<f', v2)[0]
                    items.append(item)
            else:
                break
        result['packs'] = [x for x in items if x]  # non-empty only
    return result

# State
state = {'batt_w': None, 'soc': None, 'msg_count': 0, 'replies': [], 'ci20_msgs': []}

def on_message(client, userdata, msg):
    try:
        topic = msg.topic
        payload = msg.payload
        state['msg_count'] += 1

        outer = parse_fields(payload)
        inner_bytes = outer.get(1, b'')
        if not inner_bytes:
            return
        inner = parse_fields(inner_bytes)
        cf = inner.get(8, '?')
        ci = inner.get(9, '?')
        src = inner.get(2, '?')
        ver = inner.get(16, '?')
        pdata_bytes = inner.get(1, b'') if isinstance(inner.get(1), bytes) else b''

        if 'set_reply' in topic:
            ack = decode_ack(pdata_bytes)
            tag = f"cf={cf},ci={ci},src={src},ver={ver}"
            state['replies'].append({'tag': tag, 'ack': ack, 'ci': ci})
            if ci == 18:
                print(f"\n{'='*70}")
                print(f"*** ACK ci=18 (ConfigWriteAck) on {topic} ***")
                print(f"  cf={cf}, ci={ci}, src={src}, ver={ver}")
                print(f"  ACK: {ack}")
                print(f"{'='*70}")
            elif ci == 20:
                decoded = decode_ci20(pdata_bytes)
                state['ci20_msgs'].append(decoded)
                print(f"\n--- ESG ci=20 state push ---")
                if 'packs' in decoded:
                    for p in decoded['packs']:
                        print(f"  pack: {p}")
                print(f"---")
            else:
                print(f"\n  [set_reply cf={cf} ci={ci} src={src}]: {ack}")
            return

        # Telemetry - v7 CONFIRMED parsing
        if isinstance(pdata_bytes, bytes) and len(pdata_bytes) > 0:
            pdata = parse_fields(pdata_bytes)
            # f518 = battery watts (wire type 2, 4 bytes, little-endian float)
            if 518 in pdata and isinstance(pdata[518], bytes) and len(pdata[518]) == 4:
                state['batt_w'] = struct.unpack('<f', pdata[518])[0]
            # f1009 = sub-message, field 5 = SOC float
            if 1009 in pdata and isinstance(pdata[1009], bytes):
                sub = parse_fields(pdata[1009])
                if 5 in sub and isinstance(sub[5], bytes) and len(sub[5]) == 4:
                    state['soc'] = struct.unpack('<f', sub[5])[0]

        dev = 'ESG' if SN_ESG in topic else 'DPU'
        batt = f"batt={state['batt_w']:.0f}W" if state['batt_w'] else "batt=?"
        soc  = f"soc={state['soc']:.0f}%" if state['soc'] else ""
        if state['batt_w']:
            print(f"  [{state['msg_count']:3d}] {dev} {batt} {soc}")

    except Exception as e:
        import traceback
        print(f"  [on_message error: {e}]")

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected")
        client.subscribe([(DATA_TOPIC_ESG, 1), (DATA_TOPIC_DPU, 1),
                          (SET_REPLY_ESG, 1), (SET_REPLY_DPU, 1)])
        print("Subscribed")
        client.publish(GET_TOPIC_ESG, json.dumps({
            "from": "HomeAssistant", "id": "999901234",
            "version": "1.1", "moduleType": 0,
            "operateType": "latestQuotas", "params": {}
        }), qos=1)
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

# Wait for stable telemetry
print("Connecting and waiting 15s for stable telemetry...")
time.sleep(15)

batt_now = state['batt_w']
soc_now = state['soc']
baseline_batt = batt_now or 5050
print(f"\nBaseline: batt_w={baseline_batt:.0f}W, soc={soc_now}")
print(f"ci=20 msgs received: {len(state['ci20_msgs'])}")
if state['ci20_msgs']:
    print("  Latest ci=20 packs:")
    for p in state['ci20_msgs'][-1].get('packs', []):
        print(f"    {p}")

def run_test(label, pdata_bytes, cmd_func=254, cmd_id=17, dest=11, version=3, wait=30):
    """Run a test step, wait, and report batt_w change."""
    print(f"\n{'='*70}")
    print(f">>> {label}")
    print(f"  cf={cmd_func}/ci={cmd_id}/dest={dest}/ver={version}")
    print(f"  pdata hex: {pdata_bytes.hex()}")
    acks_before = len(state['replies'])
    ci20_before = len(state['ci20_msgs'])
    payload = build_esg_cmd(pdata_bytes, cmd_func=cmd_func, cmd_id=cmd_id,
                             dest=dest, version=version)
    rc = client.publish(SET_TOPIC_ESG, payload, qos=1)
    print(f"  Published rc={rc.rc}, waiting {wait}s...")
    readings = []
    for t in range(wait):
        time.sleep(1)
        bw = state['batt_w']
        if bw: readings.append(bw)
        # Print progress every 10s
        if (t+1) % 10 == 0 and bw:
            print(f"  [{t+1:2d}s] batt_w={bw:.0f}W")

    bw_end = state['batt_w']
    delta = (bw_end - baseline_batt) if (bw_end and baseline_batt) else 0
    new_acks = state['replies'][acks_before:]
    new_ci20 = state['ci20_msgs'][ci20_before:]
    ci18_acks = [a['ack'] for a in new_acks if a.get('ci') == 18]

    print(f"  Result: batt_w={bw_end:.0f}W (delta {delta:+.0f}W vs baseline {baseline_batt:.0f}W)" if bw_end else "  Result: batt_w=None")
    if readings:
        print(f"  Min during test: {min(readings):.0f}W, Max: {max(readings):.0f}W")
    print(f"  ci=18 ACKs: {ci18_acks}")
    if new_ci20:
        print(f"  New ci=20 state (after command):")
        for m in new_ci20:
            for p in m.get('packs', []):
                print(f"    {p}")
    if bw_end and bw_end < baseline_batt - 1000:
        print(f"  *** CHARGING REDUCED BY {baseline_batt-bw_end:.0f}W ***")
    if bw_end and bw_end < 200:
        print(f"  *** CHARGING STOPPED! ***")
    return bw_end

ts = int(time.time())

# ── STEP 1: cms_max_chg_soc=80 (3% below current SOC ~83%) ──────────
# This is the most important test. 30s wait to see if charging stops.
r1 = run_test(
    "STEP 1: cms_max_chg_soc=80 (field 33, below ~83% SOC) - 30s watch",
    pb_varint(6, ts) + pb_varint(33, 80),
    wait=30
)

ts = int(time.time())

# ── STEP 2: charge_watt_power=100W (field 7) ────────────────────────
r2 = run_test(
    "STEP 2: charge_watt_power=100 (field 7) - throttle from ~5kW",
    pb_varint(6, ts) + pb_varint(7, 100),
    wait=25
)

ts = int(time.time())

# ── STEP 3: cms_min_dsg_soc=90 (field 34) - sanity check field 34 ───
# If ESG also knows field 34 (min discharge SOC), action_id=34 means ESG has DPUX schema
r3 = run_test(
    "STEP 3: cms_min_dsg_soc=90 (field 34) - schema sanity check",
    pb_varint(6, ts) + pb_varint(34, 90),
    wait=15
)

ts = int(time.time())

# ── STEP 4: ci=20 as SET command ─────────────────────────────────────
# ESG sends ci=20 state with field_129 containing pack sub-messages with f6=127
# Try sending ci=20 with field_129 sub-message f5=1, f6=80 (set pack 1 max SOC)
sub_msg = pb_varint(5, 1) + pb_varint(6, 80)   # pack_idx=1, max_soc=80
ci20_pdata = pb_bytes(129, pb_bytes(1, sub_msg))
r4 = run_test(
    "STEP 4: cf=254/ci=20 with field_129 sub-msg (send ESG's own state-push format)",
    ci20_pdata,
    cmd_func=254, cmd_id=20, dest=11, version=3,
    wait=15
)

ts = int(time.time())

# ── STEP 5: cms_max_chg_soc=50 (field 33, extreme - 33% below SOC) ──
r5 = run_test(
    "STEP 5: cms_max_chg_soc=50 (field 33, 33% below SOC) - extreme test",
    pb_varint(6, ts) + pb_varint(33, 50),
    wait=25
)

ts = int(time.time())

# ── STEP 6: RESTORE ──────────────────────────────────────────────────
print(f"\n>>> STEP 6: RESTORE (cms_max_chg_soc=100, charge_watt_power=7200, cms_min_dsg_soc=0)")
pdata_restore = pb_varint(6, ts) + pb_varint(33, 100) + pb_varint(7, 7200) + pb_varint(34, 0)
payload_r = build_esg_cmd(pdata_restore)
rc = client.publish(SET_TOPIC_ESG, payload_r, qos=1)
print(f"  Published rc={rc.rc}")
time.sleep(10)

# ── FINAL SUMMARY ────────────────────────────────────────────────────
print("\n" + "="*70)
print("FINAL SUMMARY")
print("="*70)
print(f"  Baseline batt_w : {baseline_batt:.0f}W, SOC: {soc_now}")
batt_final = state['batt_w']
print(f"  Final batt_w    : {batt_final:.0f}W" if batt_final else "  Final batt_w    : None")
print(f"\n  Effects (vs baseline {baseline_batt:.0f}W):")
for label, r in [
    ("STEP 1 cms_max_chg_soc=80", r1),
    ("STEP 2 charge_watt_power=100", r2),
    ("STEP 3 cms_min_dsg_soc=90", r3),
    ("STEP 4 ci=20 SET", r4),
    ("STEP 5 cms_max_chg_soc=50", r5),
]:
    if r:
        d = r - baseline_batt
        flag = " *** EFFECT! ***" if abs(d) > 800 else ""
        print(f"    {label}: {r:.0f}W ({d:+.0f}W){flag}")
    else:
        print(f"    {label}: None")

print(f"\n  Total ACKs: {len(state['replies'])}")
print(f"  Total ci=20 msgs: {len(state['ci20_msgs'])}")
print(f"\n  All ci=18 ACKs:")
for i, r in enumerate(state['replies']):
    if r.get('ci') == 18:
        print(f"    [{i+1}] {r['ack']}")

client.loop_stop()
client.disconnect()
