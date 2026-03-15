#!/usr/bin/env python3
"""
EcoFlow v12d - Test NEW accepted fields (f67-f72, f79) while charging active.

Key insight from v12c: Fields f67-f72 and f79 are newly discovered ACCEPTED
fields on ESG ConfigWrite. They were tested while battery was idle, so we
couldn't tell if they affect mode. This script tests them while CHARGING IS
ACTIVE so we can see if mode changes from 1 (backup) to 2 (self-powered).

Also tests the new fields with value=1 (not just 2).

HOW TO USE:
  1. Start charging from EcoFlow app
  2. Run this script — it will wait up to 5 min for charging to start
  3. Once charging detected, tests run automatically

Expected: mode=1 while charging. If any field changes mode to 2 or stops
charging (batt_w drops), THAT is the mode control field.
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

SET_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set'
SET_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/set'
DATA_ESG = f'/app/device/property/{SN_ESG}'
DATA_DPU = f'/app/device/property/{SN_DPU}'
REP_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply'
REP_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/set_reply'
GET_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/get'
GET_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/get'

def pb_varint(field, value):
    tag = (field << 3) | 0
    result = b''
    v = tag
    while v > 0x7F: result += bytes([0x80|(v&0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    v = int(value) & 0xFFFFFFFFFFFFFFFF
    while v > 0x7F: result += bytes([0x80|(v&0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    return result

def pb_bytes(field, data):
    tag = (field << 3) | 2
    result = b''
    v = tag
    while v > 0x7F: result += bytes([0x80|(v&0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    v = len(data)
    while v > 0x7F: result += bytes([0x80|(v&0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    return result + data

def pb_string(field, s): return pb_bytes(field, s.encode())

def parse_fields(payload):
    fields = {}; i = 0
    while i < len(payload):
        tag = 0; shift = 0
        try:
            while i < len(payload):
                b = payload[i]; i += 1; tag |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
        except: break
        fn = tag >> 3; wt = tag & 7
        if wt == 0:
            val = 0; shift = 0
            while i < len(payload):
                b = payload[i]; i += 1; val |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            fields[fn] = val
        elif wt == 2:
            length = 0; shift = 0
            while i < len(payload):
                b = payload[i]; i += 1; length |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            fields[fn] = payload[i:i+length]; i += length
        elif wt == 5:
            fields[fn] = payload[i:i+4]; i += 4
        else: break
    return fields

def build_esg_cmd(pdata_bytes, cf=254, ci=17, dest=11, ver=3, src=32):
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    hdr = (
        pb_bytes(1, pdata_bytes) + pb_varint(2, src) + pb_varint(3, dest) +
        pb_varint(4, 1) + pb_varint(5, 1) + pb_varint(8, cf) + pb_varint(9, ci) +
        pb_varint(10, len(pdata_bytes)) + pb_varint(11, 1) + pb_varint(14, seq) +
        pb_varint(16, ver) + pb_varint(17, 1) + pb_string(23, 'Android')
    )
    return pb_bytes(1, hdr)

def decode_ack(pdata_bytes):
    if not pdata_bytes: return {}
    p = parse_fields(pdata_bytes)
    r = {}
    if 1 in p: r['action_id'] = p[1]
    if 2 in p: r['config_ok'] = p[2]
    for k, v in p.items():
        if k not in (1, 2):
            r[f'f{k}'] = v if not isinstance(v, bytes) else v.hex()
    r['_all'] = sorted(p.keys())
    return r

state = {
    'batt_w': None, 'soc': None, 'mode': None,
    'esg_replies': [], 'dpu_replies': []
}

def on_message(client, userdata, msg):
    try:
        t = msg.topic; p = msg.payload
        outer = parse_fields(p); inner = parse_fields(outer.get(1, b''))
        cf_v = inner.get(8); ci_v = inner.get(9)
        pd_b = inner.get(1, b'') if isinstance(inner.get(1), bytes) else b''

        if t == REP_ESG:
            ack = decode_ack(pd_b)
            if ci_v != 20:
                state['esg_replies'].append({'cf': cf_v, 'ci': ci_v, 'ack': ack})
                print(f'  [ESG ACK] cf={cf_v}/ci={ci_v}: {ack}')

        elif t == REP_DPU:
            ack = decode_ack(pd_b)
            state['dpu_replies'].append({'cf': cf_v, 'ci': ci_v, 'ack': ack})
            print(f'  [DPU ACK] cf={cf_v}/ci={ci_v}: {ack}')

        elif t == DATA_ESG and pd_b:
            pdata = parse_fields(pd_b)
            if 518 in pdata and isinstance(pdata[518], bytes) and len(pdata[518]) == 4:
                bw = struct.unpack('<f', pdata[518])[0]
                if abs((state['batt_w'] or 0) - bw) > 100:
                    print(f'  batt_w changed: {state["batt_w"]} -> {bw:.0f}W')
                state['batt_w'] = bw
            if 1009 in pdata and isinstance(pdata[1009], bytes):
                sub = parse_fields(pdata[1009])
                if 5 in sub and isinstance(sub[5], bytes) and len(sub[5]) == 4:
                    state['soc'] = struct.unpack('<f', sub[5])[0]
                old_mode = state['mode']
                new_mode = sub.get(4)
                if new_mode != old_mode:
                    state['mode'] = new_mode
                    print(f'  *** MODE CHANGED: {old_mode} -> {new_mode} ***')
    except: pass

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print('Connected')
        client.subscribe([(DATA_ESG,0),(DATA_DPU,0),(REP_ESG,0),(REP_DPU,0)])
        gp = json.dumps({'from':'HomeAssistant','id':'1','version':'1.1',
                         'moduleType':0,'operateType':'latestQuotas','params':{}})
        client.publish(GET_ESG, gp, qos=1)
        client.publish(GET_DPU, gp, qos=1)
    else: print(f'Failed rc={rc}')

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                     client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
client.on_connect = on_connect; client.on_message = on_message
client.username_pw_set(MQTT_USER, MQTT_PASS)
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
client.tls_set_context(ctx); client.connect('mqtt.ecoflow.com', 8883, 60); client.loop_start()

print('='*60)
print('EcoFlow v12d - New accepted fields test (needs charging active)')
print('='*60)
print('Waiting 10s for telemetry...')
time.sleep(10)
print(f'Initial: batt={f"{state["batt_w"]:.0f}W" if state["batt_w"] else "idle"}  soc={f"{state["soc"]:.1f}%" if state["soc"] else "?"}  mode={state["mode"]}')

# Wait for charging
print('\n>>> PLEASE START CHARGING FROM THE ECOFLOW APP <<<')
print('Waiting up to 5 minutes for charging to start...')
t_wait = time.time()
while time.time() - t_wait < 300:
    time.sleep(3)
    bw = state['batt_w']
    if bw and bw > 500:
        print(f'Charging detected! batt_w={bw:.0f}W SOC={state["soc"]}% mode={state["mode"]}')
        break
    elapsed = int(time.time() - t_wait)
    if elapsed % 30 == 0 and elapsed > 0:
        print(f'  [{elapsed}s] still waiting... batt_w={bw}')
else:
    print('No charging detected in 5 min. Exiting.')
    client.loop_stop(); client.disconnect(); exit(1)

# Stabilize
time.sleep(8)
baseline_batt = state['batt_w']
baseline_mode = state['mode']
print(f'\nBaseline: batt={f"{baseline_batt:.0f}W" if baseline_batt else "idle"}  mode={baseline_mode}')
print('mode=1 = backup (grid charging ON), mode=2 = self-powered (charging stop)')

def ts(): return int(time.time())
def pdata(*extra): return pb_varint(6, ts()) + b''.join(extra)

def send_and_watch(fn, val, wait_s=15, label=''):
    """Send f{fn}={val} and watch for mode or charging change."""
    pd = pdata(pb_varint(fn, val))
    n = len(state['esg_replies'])
    client.publish(SET_ESG, build_esg_cmd(pd), qos=1)

    t0 = time.time()
    start_mode = state['mode']
    start_batt = state['batt_w']
    mode_changed = False
    charging_stopped = False

    while time.time() - t0 < wait_s:
        time.sleep(1)
        m = state['mode']
        bw = state['batt_w']
        elapsed = int(time.time() - t0)

        if m != start_mode:
            mode_changed = True
        if start_batt and (bw is None or (bw is not None and bw < start_batt * 0.4)):
            charging_stopped = True

        if elapsed % 5 == 0:
            print(f'    [{elapsed:2d}s] batt={f"{bw:.0f}W" if bw else "idle"}  mode={m}')

        if mode_changed or charging_stopped:
            break

    acks = [r['ack'] for r in state['esg_replies'][n:] if r['ci'] != 20]
    return {
        'acks': acks,
        'mode_changed': mode_changed,
        'charging_stopped': charging_stopped,
        'final_mode': state['mode'],
        'final_batt': state['batt_w']
    }

results = {}

# Test all newly discovered fields with value=2 (self-powered candidate)
NEW_FIELDS = [67, 68, 69, 70, 71, 72, 79]
print(f'\n' + '='*60)
print(f'Testing new accepted fields with value=2 (while charging at ~{baseline_batt:.0f}W)')
print('='*60)

for fn in NEW_FIELDS:
    print(f'\n--- f{fn}=2 ---')
    r = send_and_watch(fn, 2, wait_s=15)
    tag = '[WORKED]' if (r['mode_changed'] or r['charging_stopped']) else ('[ACK] ' if r['acks'] else '[silent]')
    print(f'  {tag} f{fn}=2: acks={[a.get("action_id") for a in r["acks"]]} '
          f'mode_chg={r["mode_changed"]} charging_stop={r["charging_stopped"]} '
          f'final_mode={r["final_mode"]}')
    results[f'f{fn}_2'] = r

    # Restore with value=0
    client.publish(SET_ESG, build_esg_cmd(pdata(pb_varint(fn, 0))), qos=1)
    time.sleep(4)

    if r['mode_changed'] or r['charging_stopped']:
        print(f'  *** EFFECT DETECTED! Pausing for manual review...')
        print(f'  Mode went to: {state["mode"]}')
        time.sleep(15)

# If no effect, also try value=1
print(f'\n' + '='*60)
print('Testing new accepted fields with value=1 (backup mode restore?)')
print('='*60)

for fn in NEW_FIELDS:
    print(f'\n--- f{fn}=1 ---')
    r = send_and_watch(fn, 1, wait_s=15)
    tag = '[WORKED]' if (r['mode_changed'] or r['charging_stopped']) else ('[ACK] ' if r['acks'] else '[silent]')
    print(f'  {tag} f{fn}=1: acks={[a.get("action_id") for a in r["acks"]]} '
          f'mode_chg={r["mode_changed"]} charging_stop={r["charging_stopped"]} '
          f'final_mode={r["final_mode"]}')
    results[f'f{fn}_1'] = r
    client.publish(SET_ESG, build_esg_cmd(pdata(pb_varint(fn, 0))), qos=1)
    time.sleep(3)
    if r['mode_changed'] or r['charging_stopped']:
        print(f'  *** EFFECT DETECTED on f{fn}=1!')
        time.sleep(10)

# Also try extended scan f80-f100
print(f'\n' + '='*60)
print('Quick scan f80-f100 (value=2)')
print('='*60)
new_in_80_100 = []
for fn in range(80, 101):
    pd = pdata(pb_varint(fn, 2))
    n = len(state['esg_replies'])
    client.publish(SET_ESG, build_esg_cmd(pd), qos=1)
    time.sleep(3)
    acks = [r['ack'] for r in state['esg_replies'][n:] if r['ci'] != 20]
    accepted = acks and any(a.get('action_id') == fn for a in acks)
    if accepted:
        print(f'  [OK] f{fn}=2: acks={[a.get("action_id") for a in acks]} mode={state["mode"]}')
        new_in_80_100.append(fn)
        client.publish(SET_ESG, build_esg_cmd(pdata(pb_varint(fn, 0))), qos=1)
        time.sleep(2)
print(f'Accepted in f80-f100: {new_in_80_100}')

# FINAL SUMMARY
print('\n' + '='*60)
print('FINAL SUMMARY')
print('='*60)
print(f'  Baseline: batt={f"{baseline_batt:.0f}W" if baseline_batt else "?"} mode={baseline_mode}')
print(f'  Final:    batt={f"{state["batt_w"]:.0f}W" if state["batt_w"] else "idle"} mode={state["mode"]}')
print()

any_worked = False
for key, r in results.items():
    if r.get('mode_changed') or r.get('charging_stopped'):
        any_worked = True
        print(f'  *** {key}: mode_changed={r["mode_changed"]} charging_stopped={r["charging_stopped"]} '
              f'final_mode={r["final_mode"]}')

if not any_worked:
    print('  No field produced a mode change or charging stop.')
    print()
    print('  All new accepted fields (value=2): NO MODE EFFECT')
    print('  All new accepted fields (value=1): NO MODE EFFECT')
    print()
    print('  Accepted ESG ConfigWrite fields (complete list so far):')
    print('  f5, f6, f7, f33, f34, f35, f37, f47-f50, f67-f72, f79')
    print(f'  f80-f100: {new_in_80_100}')
    print()
    print('  CONCLUSION: ESG ConfigWrite (cf=254/ci=17) does NOT control')
    print('  operating mode. Mode switching requires a different command type.')
    print()
    print('  RECOMMENDED NEXT STEPS:')
    print('  1. mitmproxy on phone — capture exact app command format')
    print('  2. EcoFlow developer REST API — try mode commands via /iot-open/')
    print('  3. Look for ci=19 or ci=21 (adjacent to ConfigWrite ci=17/18)')

client.loop_stop()
client.disconnect()
