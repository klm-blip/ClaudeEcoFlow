#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EcoFlow v12b - Mode switching continuation + broader command search.

Key findings from v12:
  - mode=1 during active charging = backup mode (grid charging ON)
  - mode=2 would be self-powered (no grid charging)
  - ESG ConfigWrite f5=2 (eps_mode_info): accepted & stored, NO mode change
  - ESG ConfigWrite f1009 sub-msg: REJECTED (high field# not accepted)
  - f10=2: REJECTED (script crashed mid-scan)

This script tests:
  SCAN A : ESG ConfigWrite f10-f29 scan (value=2), skipping known rejecteds
  TEST B : JSON cmdCode PD303_APP_SET smartBackupMode=2 on ESG consumer topic
  TEST C : JSON cmdCode PD303_APP_SET smartBackupMode=2 on DPU consumer topic
  TEST D : ESG ConfigWrite cf=12/ci=33 (shp2cmd) with various pdata
  TEST E : ESG ConfigWrite cf=254/ci=17/dest=11 higher fields: f35-f50 (value=2)
  TEST F : DPU ConfigWrite f5=2 (check what DPU accepts)

Success = mode telemetry changes from 1 to 2, OR batt_w drops >50%.
"""
import ssl, json, time, struct
import paho.mqtt.client as mqtt

SKIP_FIELDS = {2, 3, 4, 8, 9, 10, 18, 21, 30}  # known rejected

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

def pb_varint(field, value):
    tag = (field << 3) | 0
    result = b''
    v = tag
    while v > 0x7F: result += bytes([0x80|(v&0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    v = value & 0xFFFFFFFFFFFFFFFF  # handle negative as unsigned 64-bit
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

def pb_string(field, s):
    return pb_bytes(field, s.encode('utf-8'))

def build_esg_cmd(pdata_bytes, cmd_func=254, cmd_id=17, dest=11, version=3, src=32):
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    hdr = (
        pb_bytes(1, pdata_bytes) + pb_varint(2, src) + pb_varint(3, dest) +
        pb_varint(4, 1) + pb_varint(5, 1) + pb_varint(8, cmd_func) + pb_varint(9, cmd_id) +
        pb_varint(10, len(pdata_bytes)) + pb_varint(11, 1) + pb_varint(14, seq) +
        pb_varint(16, version) + pb_varint(17, 1) + pb_string(23, 'Android')
    )
    return pb_bytes(1, hdr)

def build_dpu_cmd(pdata_bytes, cmd_func=254, cmd_id=17, dest=2, version=3, src=32):
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    hdr = (
        pb_bytes(1, pdata_bytes) + pb_varint(2, src) + pb_varint(3, dest) +
        pb_varint(4, 1) + pb_varint(5, 1) + pb_varint(8, cmd_func) + pb_varint(9, cmd_id) +
        pb_varint(10, len(pdata_bytes)) + pb_varint(11, 1) + pb_varint(14, seq) +
        pb_varint(16, version) + pb_varint(17, 1) + pb_string(23, 'Android')
    )
    return pb_bytes(1, hdr)

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
    result['_fields'] = sorted(p.keys())
    return result

state = {
    'batt_w': None, 'soc': None, 'grid_w': None, 'mode': None,
    'msg_count': 0, 'dpu_replies': [], 'esg_replies': [],
    'last_mode': None,   # previous mode value for change detection
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
        cf = inner.get(8); ci = inner.get(9); src = inner.get(2)
        pdata_bytes = inner.get(1, b'') if isinstance(inner.get(1), bytes) else b''

        if topic == SET_REPLY_DPU:
            ack = decode_ack(pdata_bytes)
            state['dpu_replies'].append({'cf': cf, 'ci': ci, 'src': src, 'ack': ack})
            print(f'  [DPU ACK] cf={cf}/ci={ci}: {ack}')

        elif topic == SET_REPLY_ESG:
            ack = decode_ack(pdata_bytes)
            state['esg_replies'].append({'cf': cf, 'ci': ci, 'src': src, 'ack': ack})
            # Only print non-ci20 ACKs (ci=20 is state broadcast, noisy)
            if ci != 20:
                print(f'  [ESG ACK] cf={cf}/ci={ci}: {ack}')
            else:
                print(f'  [ESG ci=20 state push received]')

        elif topic == DATA_TOPIC_ESG:
            if isinstance(pdata_bytes, bytes) and len(pdata_bytes) > 0:
                pdata = parse_fields(pdata_bytes)
                if 518 in pdata and isinstance(pdata[518], bytes) and len(pdata[518]) == 4:
                    state['batt_w'] = struct.unpack('<f', pdata[518])[0]
                if 515 in pdata and isinstance(pdata[515], bytes) and len(pdata[515]) == 4:
                    state['grid_w'] = struct.unpack('<f', pdata[515])[0]
                if 1009 in pdata and isinstance(pdata[1009], bytes):
                    sub = parse_fields(pdata[1009])
                    if 5 in sub and isinstance(sub[5], bytes) and len(sub[5]) == 4:
                        state['soc'] = struct.unpack('<f', sub[5])[0]
                    old_mode = state['mode']
                    new_mode = sub.get(4)  # varint; None if absent
                    if new_mode != old_mode:
                        state['last_mode'] = old_mode
                        state['mode'] = new_mode
                        print(f'  *** MODE CHANGE: {old_mode} -> {new_mode} ***')
    except Exception as e:
        print(f'  [parse err: {e}]')

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print('Connected')
        client.subscribe([
            (DATA_TOPIC_ESG, 0), (DATA_TOPIC_DPU, 0),
            (SET_REPLY_DPU, 0), (SET_REPLY_ESG, 0)
        ])
        gp = json.dumps({'from':'HomeAssistant','id':'99','version':'1.1',
                         'moduleType':0,'operateType':'latestQuotas','params':{}})
        client.publish(GET_TOPIC_ESG, gp, qos=1)
        client.publish(GET_TOPIC_DPU, gp, qos=1)
    else:
        print(f'Connect failed rc={rc}')

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
client.on_connect = on_connect
client.on_message = on_message
client.username_pw_set(MQTT_USER, MQTT_PASS)
ctx = ssl.create_default_context()
ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
client.tls_set_context(ctx)
client.connect('mqtt.ecoflow.com', 8883, 60)
client.loop_start()

print('='*65)
print('EcoFlow v12b - Mode Switch Search (continuation)')
print('='*65)
print('Waiting 12s for telemetry...')
time.sleep(12)

baseline_batt = state['batt_w']
baseline_mode = state['mode']
baseline_soc  = state['soc']
charging = baseline_batt is not None and baseline_batt > 500
print(f'\nState: batt={f"{baseline_batt:.0f}W" if baseline_batt else "idle"}  '
      f'soc={f"{baseline_soc:.1f}%" if baseline_soc else "?"}  '
      f'mode={baseline_mode}  charging={charging}')
print(f'  mode=1 = backup mode (grid charging), mode=2 = self-powered (stop charging)')

def ts(): return int(time.time())

def pdata(*fields): return pb_varint(6, ts()) + b''.join(fields)

def send_esg_cmd(pdata_bytes, wait_s=6):
    n = len(state['esg_replies'])
    client.publish(SET_TOPIC_ESG, build_esg_cmd(pdata_bytes), qos=1)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(0.3)
        new = state['esg_replies'][n:]
        if new and any(r['ci'] != 20 for r in new):
            return [r['ack'] for r in new if r['ci'] != 20]
    return [r['ack'] for r in state['esg_replies'][n:] if r['ci'] != 20]

def send_dpu_cmd(pdata_bytes, wait_s=6):
    n = len(state['dpu_replies'])
    client.publish(SET_TOPIC_DPU, build_dpu_cmd(pdata_bytes), qos=1)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(0.3)
        new = state['dpu_replies'][n:]
        if new: return [r['ack'] for r in new]
    return [r['ack'] for r in state['dpu_replies'][n:]]

def snap(label='', watch_s=12):
    """Watch for mode or charging change for watch_s seconds. Return summary."""
    start_mode = state['mode']
    start_batt = state['batt_w']
    t0 = time.time()
    while time.time() - t0 < watch_s:
        time.sleep(2)
        bw = state['batt_w']
        m  = state['mode']
        elapsed = int(time.time() - t0)
        if elapsed % 4 == 0:
            print(f'    [{elapsed:2d}s] batt={f"{bw:.0f}W" if bw else "None"}  mode={m}')
        # Early exit if mode changed
        if m != start_mode:
            print(f'  *** MODE CHANGED: {start_mode} -> {m} ***')
            return {'mode_changed': True, 'batt_changed': False, 'new_mode': m,
                    'final_batt': bw, 'start_mode': start_mode}
    end_batt = state['batt_w']
    batt_changed = (start_batt and end_batt and
                    abs(end_batt - start_batt) > start_batt * 0.5)
    return {'mode_changed': False, 'batt_changed': batt_changed,
            'new_mode': state['mode'], 'final_batt': end_batt, 'start_mode': start_mode}

def restore_mode():
    """Send f5=0 to make sure eps_mode_info is clear."""
    client.publish(SET_TOPIC_ESG, build_esg_cmd(pdata(pb_varint(5, 0))), qos=1)
    time.sleep(2)

# =====================================================================
# SCAN A: ConfigWrite f11-f29 (f10 already confirmed rejected in v12)
# =====================================================================
print('\n' + '='*65)
print('SCAN A: ESG ConfigWrite f11-f29 scan (value=2, then value=1)')
print('='*65)
scan_a = {}
for fn in range(11, 30):
    if fn in SKIP_FIELDS:
        print(f'  skip f{fn} (known rejected)')
        continue
    # Try value=2 first (self-powered mode candidate)
    acks = send_esg_cmd(pdata(pb_varint(fn, 2)), wait_s=5)
    obs  = snap(f'f{fn}=2', watch_s=10)
    accepted = acks and any(a.get('action_id') == fn for a in acks)
    mode_chg = obs['mode_changed']
    tag = '[WORKED]' if mode_chg else ('[OK]' if accepted else '[REJ]')
    print(f'  {tag} f{fn}=2 : acks={[a.get("action_id") for a in acks]} mode_chg={mode_chg}')
    scan_a[fn] = {'acks': acks, 'accepted': accepted, 'mode_chg': mode_chg, 'obs': obs}
    # Restore with value=0 regardless
    send_esg_cmd(pdata(pb_varint(fn, 0)), wait_s=3)
    time.sleep(2)

# =====================================================================
# TEST B: JSON cmdCode PD303_APP_SET smartBackupMode=2 on ESG topic
# =====================================================================
print('\n' + '='*65)
print('TEST B: JSON PD303_APP_SET smartBackupMode=2 on ESG consumer topic')
print('  (was tried on dev MQTT before; now trying on consumer /set topic)')
print('='*65)
n_esg = len(state['esg_replies'])
payload_b = json.dumps({
    'cmdCode': 'PD303_APP_SET',
    'params': {'smartBackupMode': 2}
})
client.publish(SET_TOPIC_ESG, payload_b, qos=1)
print(f'  Published JSON: {payload_b}')
obs_b = snap('TEST B', watch_s=15)
new_esg_b = state['esg_replies'][n_esg:]
print(f'  Result: mode_chg={obs_b["mode_changed"]} '
      f'replies={[r["ack"] for r in new_esg_b]}')

# Restore
payload_restore = json.dumps({'cmdCode': 'PD303_APP_SET', 'params': {'smartBackupMode': 1}})
client.publish(SET_TOPIC_ESG, payload_restore, qos=1)
time.sleep(3)

# =====================================================================
# TEST C: JSON cmdCode PD303_APP_SET smartBackupMode=2 on DPU topic
# =====================================================================
print('\n' + '='*65)
print('TEST C: JSON PD303_APP_SET smartBackupMode=2 on DPU consumer topic')
print('='*65)
n_dpu = len(state['dpu_replies'])
client.publish(SET_TOPIC_DPU, payload_b, qos=1)
print(f'  Published JSON: {payload_b}')
obs_c = snap('TEST C', watch_s=15)
new_dpu_c = state['dpu_replies'][n_dpu:]
print(f'  Result: mode_chg={obs_c["mode_changed"]} '
      f'replies={[r["ack"] for r in new_dpu_c]}')

# =====================================================================
# TEST D: ESG shp2cmd (cf=12, ci=33) with smartBackupMode field
# This is the SHP2-specific command channel referenced in ioBroker.
# Proto fields uncertain; try several layouts.
# =====================================================================
print('\n' + '='*65)
print('TEST D: ESG cf=12/ci=33 (shp2cmd) with mode field candidates')
print('='*65)
# Build shp2cmd: cf=12/ci=33, no cfgUtcTime needed (different format)
# Try several pdata layouts — unknown field mapping
def build_shp2cmd(pdata_bytes, dest=11, version=3):
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    hdr = (
        pb_bytes(1, pdata_bytes) + pb_varint(2, 32) + pb_varint(3, dest) +
        pb_varint(4, 1) + pb_varint(5, 1) + pb_varint(8, 12) + pb_varint(9, 33) +
        pb_varint(10, len(pdata_bytes)) + pb_varint(11, 1) + pb_varint(14, seq) +
        pb_varint(16, version) + pb_varint(17, 1) + pb_string(23, 'Android')
    )
    return pb_bytes(1, hdr)

# smartBackupMode candidates: try f1=2, f2=2, f3=2, f4=2
for fn, val in [(1, 2), (2, 2), (3, 2), (4, 2), (5, 2), (10, 2), (20, 2),
                (1, 1), (3, 1)]:
    pdata_d = pb_varint(fn, val)
    n_esg = len(state['esg_replies'])
    client.publish(SET_TOPIC_ESG, build_shp2cmd(pdata_d), qos=1)
    time.sleep(3)
    new_esg = state['esg_replies'][n_esg:]
    m = state['mode']
    got_ack = len(new_esg) > 0
    tag = '[REPLY]' if got_ack else '[silent]'
    print(f'  {tag} shp2cmd f{fn}={val}: mode={m} '
          f'replies={[r["ack"] for r in new_esg]}')
    if obs_b["mode_changed"] or (m is not None and m != baseline_mode):
        print(f'  *** MODE CHANGED to {m} during shp2cmd! ***')
        break

# =====================================================================
# TEST E: ESG ConfigWrite higher fields f35-f50 (value=2)
# =====================================================================
print('\n' + '='*65)
print('TEST E: ESG ConfigWrite f35-f50 (value=2) — above known fields')
print('='*65)
scan_e = {}
for fn in range(35, 51):
    if fn in (33, 34): continue
    acks = send_esg_cmd(pdata(pb_varint(fn, 2)), wait_s=4)
    accepted = acks and any(a.get('action_id') == fn for a in acks)
    m = state['mode']
    mode_chg = (m != baseline_mode)
    tag = '[WORKED]' if mode_chg else ('[OK]' if accepted else '[REJ]')
    print(f'  {tag} f{fn}=2 : acks={[a.get("action_id") for a in acks]} mode={m}')
    scan_e[fn] = {'accepted': accepted, 'mode_chg': mode_chg}
    send_esg_cmd(pdata(pb_varint(fn, 0)), wait_s=2)
    time.sleep(1)

# =====================================================================
# TEST F: DPU ConfigWrite f5=2
# =====================================================================
print('\n' + '='*65)
print('TEST F: DPU ConfigWrite f5=2 (check DPU mode field)')
print('='*65)
acks_f = send_dpu_cmd(pdata(pb_varint(5, 2)))
obs_f  = snap('DPU f5=2', watch_s=12)
print(f'  DPU ACKs: {acks_f}  mode_chg={obs_f["mode_changed"]}')
send_dpu_cmd(pdata(pb_varint(5, 0)))
time.sleep(3)

# =====================================================================
# TEST G: ESG ConfigWrite with BOTH f5=2 AND f7=0 (eps_mode + stop charge)
# =====================================================================
print('\n' + '='*65)
print('TEST G: ESG ConfigWrite f5=2 + f7=0 (eps_mode=2, charge_watts=0)')
print('  Combining accepted fields — maybe both needed together.')
print('='*65)
acks_g = send_esg_cmd(pdata(pb_varint(5, 2), pb_varint(7, 0)))
obs_g  = snap('TEST G', watch_s=15)
print(f'  ACKs: {acks_g}  mode_chg={obs_g["mode_changed"]}')
# Restore
send_esg_cmd(pdata(pb_varint(5, 0), pb_varint(7, 7200)))
time.sleep(3)

# =====================================================================
# TEST H: ESG ConfigWrite f5=2 + f33=100 (eps=2, max_chg_soc=100)
# Maybe EPS mode=2 + SOC=100 triggers self-powered
# =====================================================================
print('\n' + '='*65)
print('TEST H: ESG ConfigWrite f5=2 + f33=100')
print('='*65)
acks_h = send_esg_cmd(pdata(pb_varint(5, 2), pb_varint(33, 100)))
obs_h  = snap('TEST H', watch_s=15)
print(f'  ACKs: {acks_h}  mode_chg={obs_h["mode_changed"]}')
send_esg_cmd(pdata(pb_varint(5, 0)))
time.sleep(3)

# =====================================================================
# FINAL SUMMARY
# =====================================================================
print('\n' + '='*65)
print('FINAL SUMMARY')
print('='*65)
print(f'  Initial mode: {baseline_mode}  Final mode: {state["mode"]}')
print(f'  Initial batt: {f"{baseline_batt:.0f}W" if baseline_batt else "idle"}  '
      f'Final batt: {f"{state["batt_w"]:.0f}W" if state["batt_w"] else "idle"}')

print('\n  SCAN A results:')
accepted_fields = []
for fn, v in scan_a.items():
    if v['accepted'] or v['mode_chg']:
        tag = '[WORKED]' if v['mode_chg'] else '[OK]'
        print(f'    {tag} f{fn}: acks={[a.get("action_id") for a in v["acks"]]}')
        accepted_fields.append(fn)
if not accepted_fields:
    print('    All rejected (action_id fell back to 6 for all)')

print('\n  TEST B (JSON smartBackupMode=2 on ESG topic):')
print(f'    mode_chg={obs_b["mode_changed"]}  replies present={len(new_esg_b)>0}')

print('\n  TEST C (JSON smartBackupMode=2 on DPU topic):')
print(f'    mode_chg={obs_c["mode_changed"]}  replies present={len(new_dpu_c)>0}')

print('\n  SCAN E (f35-f50) accepted fields:')
e_accepted = [fn for fn, v in scan_e.items() if v['accepted']]
print(f'    {e_accepted if e_accepted else "none"}')

print('\n  ESG ConfigWrite known accepted fields (all tests combined):')
print('    f5 (eps_mode_info), f6 (cfgUtcTime), f7 (charge_watt_power)')
print('    f33 (cms_max_chg_soc), f34 (cms_min_dsg_soc)')
print('    + any new ones from this run above')

print(f'\n  Total ESG replies: {len(state["esg_replies"])}')
print(f'  Total DPU replies: {len(state["dpu_replies"])}')
print(f'  Total msgs: {state["msg_count"]}')

client.loop_stop()
client.disconnect()
