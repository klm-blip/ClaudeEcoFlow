#!/usr/bin/env python3
"""
EcoFlow charge control v8 - ESG targeted tests after v7 analysis

v7 findings:
- field 33 (cms_max_chg_soc=0): accepted (action_id=33) but NO effect
  -> 0 is proto3 default, ESG treats it as "no limit / unset"
- field 7 (charge_watt_power=500): accepted (action_id=7) but NO effect
  -> 500W should have throttled 5050W charge; didn't. Unit mismatch? Or 0-effect?
- field 30, field 3: REJECTED (action_id fell back to 6) - not in ESG schema

v8 tests:
- STEP 1: cms_max_chg_soc=80 (non-zero, below current SOC ~83%) - force stop
- STEP 2: cms_max_chg_soc=1  (near-zero non-default - should definitely stop if field works)
- STEP 3: charge_watt_power=100 (very low - if units are watts, should drop 5000W -> 100W)
- STEP 4: charge_watt_power=10  (minimum - try to stop via power cap)
- STEP 5: cmdFunc=2/cmdId=87/chgMaxSoc=80 (DPUX's actual chgMaxSoc command targeting ESG)
- STEP 6: cms_max_chg_soc=82 + charge_watt_power=10 together (combined attack)
- STEP 7: Restore - cms_max_chg_soc=100 + charge_watt_power=7200

Current SOC = ~83%, charging at ~5050W
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
    while length > 0x7F: result += bytes([0x80|(length&0x7F)]); length >>= 7
    result += bytes([length & 0x7F])
    return result + data

def pb_string(field, s):
    return pb_bytes(field, s.encode('utf-8'))

def build_esg_cmd(pdata_bytes, cmd_func=254, cmd_id=17, dest=11, version=3):
    """Build ConfigWrite for ESG: dest=11, cmdFunc=254, cmdId=17, version=3."""
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    header = (
        pb_bytes(1, pdata_bytes) + pb_varint(2, 32) + pb_varint(3, dest) +
        pb_varint(4, 1) + pb_varint(5, 1) + pb_varint(8, cmd_func) + pb_varint(9, cmd_id) +
        pb_varint(10, len(pdata_bytes)) + pb_varint(11, 1) + pb_varint(14, seq) +
        pb_varint(16, version) + pb_varint(17, 1) + pb_string(23, 'Android')
    )
    return pb_bytes(1, header)

def parse_varint(data, i):
    val = 0; shift = 0
    while i < len(data):
        b = data[i]; i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80): break
        shift += 7
    return val, i

def parse_fields(data):
    fields = {}; i = 0
    while i < len(data):
        if i >= len(data): break
        try:
            tag, i = parse_varint(data, i)
        except: break
        fn = tag >> 3; wt = tag & 7
        if wt == 0:
            val, i = parse_varint(data, i)
            fields[fn] = val
        elif wt == 2:
            length, i = parse_varint(data, i)
            fields[fn] = data[i:i+length]; i += length
        elif wt == 5:
            fields[fn] = struct.unpack('<f', data[i:i+4])[0]; i += 4
        else:
            break
    return fields

def decode_ack(pdata):
    """Decode ConfigWriteAck pdata (ci=18): {action_id, config_ok}"""
    f = parse_fields(pdata)
    result = {}
    if 1 in f: result['action_id'] = f[1]
    if 2 in f: result['config_ok'] = f[2]
    # Also capture any unknown fields
    for k, v in f.items():
        if k not in (1, 2):
            result[f'field_{k}'] = v
    return result

def extract_batt_w(data):
    """Extract battery watts from ESG/DPU telemetry."""
    try:
        f = parse_fields(data)
        if 1 not in f: return None
        inner = parse_fields(f[1])
        pdata_raw = inner.get(1, b'')
        if not pdata_raw: return None
        pf = parse_fields(pdata_raw)
        if not pf: return None
        # Walk nested fields for f518 (battery watts float)
        def find_float_field(d, target_key):
            for k, v in d.items():
                if isinstance(v, bytes):
                    sub = parse_fields(v)
                    result = find_float_field(sub, target_key)
                    if result is not None:
                        return result
                elif isinstance(v, float) and abs(v) > 10:
                    if k == target_key:
                        return v
            return None
        bw = find_float_field(pf, 518)
        # Also try soc
        def find_soc(d):
            for k, v in d.items():
                if isinstance(v, bytes):
                    sub = parse_fields(v)
                    r = find_soc(sub)
                    if r is not None: return r
                elif isinstance(v, float) and 0 < v <= 100:
                    if k in (1009, 5):
                        return v
            return None
        return bw
    except:
        return None

def extract_soc(data):
    try:
        f = parse_fields(data)
        if 1 not in f: return None
        inner = parse_fields(f[1])
        pdata_raw = inner.get(1, b'')
        if not pdata_raw: return None
        pf = parse_fields(pdata_raw)
        def find_soc_nested(d, depth=0):
            if depth > 6: return None
            for k, v in d.items():
                if isinstance(v, bytes) and len(v) > 0:
                    sub = parse_fields(v)
                    r = find_soc_nested(sub, depth+1)
                    if r is not None: return r
                elif isinstance(v, float) and 0.1 < v <= 100.0:
                    if k == 5: return v
            return None
        return find_soc_nested(pf)
    except:
        return None

# State
telemetry = {'esg_batt': None, 'dpu_batt': None, 'soc': None, 'count': 0}
acks = []
baseline_batt = None

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected")
        client.subscribe(DATA_TOPIC_ESG, qos=1)
        client.subscribe(DATA_TOPIC_DPU, qos=1)
        client.subscribe(SET_REPLY_ESG, qos=1)
        client.subscribe(SET_REPLY_DPU, qos=1)
        print("Subscribed")
        # Trigger telemetry
        client.publish(GET_TOPIC_ESG, json.dumps({
            "from": "HomeAssistant", "id": "999901234",
            "version": "1.1", "moduleType": 0,
            "operateType": "latestQuotas", "params": {}
        }), qos=1)
    else:
        print(f"Connect failed rc={rc}")

def on_message(client, userdata, msg):
    try:
        topic = msg.topic
        data = msg.payload

        if topic in (DATA_TOPIC_ESG, DATA_TOPIC_DPU):
            bw = extract_batt_w(data)
            soc = extract_soc(data)
            src = 'ESG' if topic == DATA_TOPIC_ESG else 'DPU'
            if bw is not None:
                telemetry['count'] += 1
                if src == 'ESG':
                    telemetry['esg_batt'] = bw
                else:
                    telemetry['dpu_batt'] = bw
                if soc: telemetry['soc'] = soc
                print(f"  [{telemetry['count']:3d}] {src} batt={bw:.0f}W soc={soc:.0f}%" if soc else
                      f"  [{telemetry['count']:3d}] {src} batt={bw:.0f}W")

        elif topic in (SET_REPLY_ESG, SET_REPLY_DPU):
            src = 'ESG' if topic == SET_REPLY_ESG else 'DPU'
            f = parse_fields(data)
            pdata_raw = f.get(1, b'')
            inner = parse_fields(pdata_raw) if pdata_raw else {}
            pdata2 = inner.get(1, b'')
            cf = inner.get(8); ci = inner.get(9)
            ver = inner.get(16); src_dev = inner.get(2)
            ack_data = decode_ack(pdata2) if pdata2 else {}
            acks.append({'src': src, 'cf': cf, 'ci': ci, 'ver': ver, 'ack': ack_data})
            print(f"\n{'='*70}")
            print(f"*** {src} ACK on {topic} ***")
            print(f"  cf={cf}, ci={ci}, src={src_dev}, ver={ver}")
            print(f"  ACK: {ack_data}")
            print(f"{'='*70}")
    except Exception as e:
        print(f"  [on_message error: {e}]")

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

# Wait for baseline
print("Waiting 12s for baseline telemetry...")
time.sleep(12)

batt_now = telemetry['esg_batt'] or telemetry['dpu_batt']
soc_now = telemetry['soc']
baseline_batt = batt_now or 5050
print(f"\nBaseline: batt_w={baseline_batt}, soc={soc_now}")
if baseline_batt and baseline_batt > 100:
    print(f"  -> Battery is CHARGING at {baseline_batt:.0f}W")
else:
    print(f"  -> Battery not charging (or no telemetry)")

def run_esg_test(label, pdata_bytes, cmd_func=254, cmd_id=17, dest=11, version=3):
    print(f"\n>>> {label}")
    print(f"  pdata hex: {pdata_bytes.hex()}")
    payload = build_esg_cmd(pdata_bytes, cmd_func=cmd_func, cmd_id=cmd_id, dest=dest, version=version)
    acks_before = len(acks)
    rc = client.publish(SET_TOPIC_ESG, payload, qos=1)
    print(f"  Published rc={rc.rc}")
    # Wait and watch battery
    for _ in range(12):
        time.sleep(1)
    bw = telemetry['esg_batt'] or telemetry['dpu_batt']
    delta = (bw - baseline_batt) if (bw and baseline_batt) else 0
    new_acks = acks[acks_before:]
    ci18_acks = [a for a in new_acks if a['ci'] == 18]
    print(f"  batt_w: {bw:.0f}W (delta: {delta:+.0f}W vs baseline {baseline_batt:.0f}W)" if bw else "  batt_w: None")
    print(f"  ci=18 ACKs: {[a['ack'] for a in ci18_acks]}")
    # Check for meaningful drop
    if bw and baseline_batt and bw < baseline_batt - 1000:
        print(f"  *** CHARGING REDUCED! Drop of {baseline_batt - bw:.0f}W ***")
    elif bw and bw < 200:
        print(f"  *** CHARGING STOPPED! batt_w={bw:.0f}W ***")
    return bw

print("\n" + "="*70)
print("ESG CHARGE CONTROL v8 - Targeted tests")
print("="*70)

ts = int(time.time())

# STEP 1: cms_max_chg_soc=80 (NON-ZERO, 3% below current SOC 83%)
# If field 33 = max charge SOC and 83% > 80%, should stop charging immediately
r1 = run_esg_test(
    "STEP 1: cms_max_chg_soc=80 (non-zero, below 83% SOC)",
    pb_varint(6, ts) + pb_varint(33, 80)
)

ts = int(time.time())

# STEP 2: cms_max_chg_soc=1 (extreme non-zero - 1% max, should definitely stop)
r2 = run_esg_test(
    "STEP 2: cms_max_chg_soc=1 (near-zero, should force stop if field active)",
    pb_varint(6, ts) + pb_varint(33, 1)
)

ts = int(time.time())

# STEP 3: charge_watt_power=100 (if in watts, 100W << 5050W charging = clear reduction)
r3 = run_esg_test(
    "STEP 3: charge_watt_power=100 (if units=watts, should drop from 5050W)",
    pb_varint(6, ts) + pb_varint(7, 100)
)

ts = int(time.time())

# STEP 4: charge_watt_power=10 (minimum - if any value controls power, 10 should show it)
r4 = run_esg_test(
    "STEP 4: charge_watt_power=10 (minimum power test)",
    pb_varint(6, ts) + pb_varint(7, 10)
)

ts = int(time.time())

# STEP 5: cmdFunc=2, cmdId=87 - DPUX's actual chgMaxSoc command targeting ESG
# In ioBroker DPUX data: chgMaxSoc: { msg: { dest: 2, cmdFunc: 2, cmdId: 87, dataLen: 2 } }
# pdata: field 1 = chgMaxSoc value (int)
# Try dest=11 (ESG address) with this command format
print(f"\n>>> STEP 5: cmdFunc=2/cmdId=87 (DPUX chgMaxSoc format, targeting ESG dest=11)")
pdata5 = pb_varint(1, 80)  # chgMaxSoc=80% in DPUX format
print(f"  pdata hex: {pdata5.hex()}")
payload5 = build_esg_cmd(pdata5, cmd_func=2, cmd_id=87, dest=11, version=3)
acks_before = len(acks)
rc = client.publish(SET_TOPIC_ESG, payload5, qos=1)
print(f"  Published rc={rc.rc}")
for _ in range(12):
    time.sleep(1)
bw5 = telemetry['esg_batt'] or telemetry['dpu_batt']
delta5 = (bw5 - baseline_batt) if (bw5 and baseline_batt) else 0
new_acks5 = acks[acks_before:]
print(f"  batt_w: {bw5:.0f}W (delta: {delta5:+.0f}W)" if bw5 else "  batt_w: None")
print(f"  ACKs: {[a['ack'] for a in new_acks5]}")

ts = int(time.time())

# STEP 6: Combined - cms_max_chg_soc=82 + charge_watt_power=10 together
r6 = run_esg_test(
    "STEP 6: COMBINED cms_max_chg_soc=82 + charge_watt_power=10",
    pb_varint(6, ts) + pb_varint(33, 82) + pb_varint(7, 10)
)

ts = int(time.time())

# STEP 7: Also try ch1_force_charge=0 (field 18 in ProtoPushAndSet - force charge OFF)
# This field exists in SHP2 schema. ESG might support it too.
r7 = run_esg_test(
    "STEP 7: ch1_force_charge=0 (field 18) - SHP2 force charge OFF",
    pb_varint(6, ts) + pb_varint(18, 0)
)

ts = int(time.time())

# STEP 8: ch1_force_charge=2 (field 18) - FORCE_CHARGE_OFF is typically value 2 in SHP2
r8 = run_esg_test(
    "STEP 8: ch1_force_charge=2 (field 18, value=FORCE_CHARGE_OFF)",
    pb_varint(6, ts) + pb_varint(18, 2)
)

ts = int(time.time())

# STEP 9: RESTORE - cms_max_chg_soc=100 + charge_watt_power=7200
print(f"\n>>> STEP 9: RESTORE (cms_max_chg_soc=100, charge_watt_power=7200)")
pdata9 = pb_varint(6, ts) + pb_varint(33, 100) + pb_varint(7, 7200)
payload9 = build_esg_cmd(pdata9)
rc = client.publish(SET_TOPIC_ESG, payload9, qos=1)
print(f"  Published restore rc={rc.rc}")
time.sleep(8)

# Final summary
print("\n" + "="*70)
print("FINAL RESULTS SUMMARY")
print("="*70)
print(f"  Baseline batt_w : {baseline_batt:.0f}W")
batt_final = telemetry['esg_batt'] or telemetry['dpu_batt']
print(f"  Final batt_w    : {batt_final:.0f}W" if batt_final else "  Final batt_w: None")
print(f"  SOC             : {telemetry['soc']}")
print(f"\n  Step results (vs baseline {baseline_batt:.0f}W):")
for i, (label, r) in enumerate([
    ("STEP 1 cms_max_chg_soc=80", r1),
    ("STEP 2 cms_max_chg_soc=1", r2),
    ("STEP 3 charge_watt_power=100", r3),
    ("STEP 4 charge_watt_power=10", r4),
    ("STEP 6 combined", r6),
    ("STEP 7 ch1_force_charge=0", r7),
    ("STEP 8 ch1_force_charge=2", r8),
], 1):
    if r:
        delta = r - baseline_batt
        flag = " *** EFFECT! ***" if abs(delta) > 500 else ""
        print(f"    {label}: {r:.0f}W ({delta:+.0f}W){flag}")

print(f"\n  All ACKs ({len(acks)} total):")
for i, a in enumerate(acks):
    print(f"    [{i+1}] cf={a['cf']}, ci={a['ci']}, src={a.get('src')}: {a['ack']}")

client.loop_stop()
client.disconnect()
