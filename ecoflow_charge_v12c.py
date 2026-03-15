#!/usr/bin/env python3
"""
EcoFlow v12c - Targeted mode-switch tests + full f1009 decode

Key new hypotheses to test:
  1. f1009 as VARINT (wire type 0), not sub-message — maybe mode IS field 1009 directly
  2. Scan f51-f80 ConfigWrite fields
  3. Different cmdFunc values: cf=2/ci=87 (DPUX native), cf=64/ci=17, cf=3/ci=17
  4. ConfigWrite dest=0 (broadcast) — different routing
  5. Full decode of f1009 sub-message contents to understand the schema
  6. Try 'latestQuotas' GET-style trigger with forceSet intent

Battery is currently idle after user stopped charging.
Mode was None (absent from f1009.f4) after charging stopped.
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

def build_cmd(pdata_bytes, topic_sn, cf=254, ci=17, dest=11, version=3, src=32):
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    hdr = (
        pb_bytes(1, pdata_bytes) + pb_varint(2, src) + pb_varint(3, dest) +
        pb_varint(4, 1) + pb_varint(5, 1) + pb_varint(8, cf) + pb_varint(9, ci) +
        pb_varint(10, len(pdata_bytes)) + pb_varint(11, 1) + pb_varint(14, seq) +
        pb_varint(16, version) + pb_varint(17, 1) + pb_string(23, 'Android')
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
    'f1009_full': None,   # raw bytes of f1009 sub-message
    'msg_count': 0,
    'esg_replies': [], 'dpu_replies': []
}

def on_message(client, userdata, msg):
    try:
        t = msg.topic; p = msg.payload
        state['msg_count'] += 1
        outer = parse_fields(p); inner = parse_fields(outer.get(1, b''))
        cf_v = inner.get(8); ci_v = inner.get(9); src_v = inner.get(2)
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

        elif t == DATA_ESG:
            if pd_b:
                pdata = parse_fields(pd_b)
                if 518 in pdata and isinstance(pdata[518], bytes) and len(pdata[518]) == 4:
                    state['batt_w'] = struct.unpack('<f', pdata[518])[0]
                if 1009 in pdata and isinstance(pdata[1009], bytes):
                    sub_b = pdata[1009]
                    state['f1009_full'] = sub_b
                    sub = parse_fields(sub_b)
                    if 5 in sub and isinstance(sub[5], bytes) and len(sub[5]) == 4:
                        state['soc'] = struct.unpack('<f', sub[5])[0]
                    old_mode = state['mode']
                    new_mode = sub.get(4)
                    if new_mode != old_mode:
                        state['mode'] = new_mode
                        print(f'  *** MODE {old_mode} -> {new_mode} ***')
    except Exception as e:
        pass

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print('Connected')
        client.subscribe([(DATA_ESG,0),(DATA_DPU,0),(REP_ESG,0),(REP_DPU,0)])
        gp = json.dumps({'from':'HomeAssistant','id':'1','version':'1.1',
                         'moduleType':0,'operateType':'latestQuotas','params':{}})
        client.publish(GET_ESG, gp, qos=1)
        client.publish(GET_DPU, gp, qos=1)
    else:
        print(f'Connect failed rc={rc}')

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                     client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
client.on_connect = on_connect; client.on_message = on_message
client.username_pw_set(MQTT_USER, MQTT_PASS)
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
client.tls_set_context(ctx); client.connect('mqtt.ecoflow.com', 8883, 60); client.loop_start()

print('='*60)
print('EcoFlow v12c - Targeted mode-switch tests')
print('='*60)
print('Waiting 12s for telemetry...')
time.sleep(12)

bw = state['batt_w']; soc = state['soc']; m = state['mode']
print(f'State: batt={f"{bw:.0f}W" if bw else "idle"}  soc={f"{soc:.1f}%" if soc else "?"}  mode={m}')

# Decode f1009 sub-message fully
if state['f1009_full']:
    print('\nf1009 sub-message FULL decode:')
    sub = parse_fields(state['f1009_full'])
    for fn, fv in sorted(sub.items()):
        if isinstance(fv, bytes) and len(fv) == 4:
            try: fv_f = struct.unpack('<f', fv)[0]; fv_str = f'{fv.hex()} = float({fv_f:.3f})'
            except: fv_str = fv.hex()
        elif isinstance(fv, bytes):
            fv_str = fv.hex()
            try:
                inner = parse_fields(fv); fv_str += f' sub={dict(inner)}'
            except: pass
        else:
            fv_str = str(fv)
        print(f'  f{fn} = {fv_str}')
else:
    print('f1009 not received yet (battery idle — f1009 may only appear during charging)')

def ts(): return int(time.time())
def pdata(*extra): return pb_varint(6, ts()) + b''.join(extra)

def send_esg(pd, cf=254, ci=17, dest=11, ver=3, wait_s=5, label=''):
    n = len(state['esg_replies'])
    payload = build_cmd(pd, SN_ESG, cf=cf, ci=ci, dest=dest, version=ver)
    client.publish(SET_ESG, payload, qos=1)
    t0 = time.time()
    while time.time() - t0 < wait_s:
        time.sleep(0.3)
        new = [r for r in state['esg_replies'][n:] if r['ci'] != 20]
        if new: return [r['ack'] for r in new]
    return [r['ack'] for r in state['esg_replies'][n:] if r['ci'] != 20]

def send_dpu(pd, cf=254, ci=17, dest=2, ver=3, wait_s=5):
    n = len(state['dpu_replies'])
    payload = build_cmd(pd, SN_DPU, cf=cf, ci=ci, dest=dest, version=ver)
    client.publish(SET_DPU, payload, qos=1)
    t0 = time.time()
    while time.time() - t0 < wait_s:
        time.sleep(0.3)
        new = state['dpu_replies'][n:]
        if new: return [r['ack'] for r in new]
    return [r['ack'] for r in state['dpu_replies'][n:]]

def quick_watch(seconds=8, label=''):
    t0 = time.time()
    start_mode = state['mode']
    while time.time() - t0 < seconds:
        time.sleep(1)
    m = state['mode']
    return {'mode_changed': m != start_mode, 'mode': m}

results = {}

# =====================================================================
# TEST 1: f1009 as VARINT (wire type 0) in ConfigWrite pdata
# Maybe mode IS a direct varint at high field 1009, not a sub-message
# =====================================================================
print('\n' + '='*60)
print('TEST 1: ESG ConfigWrite f1009 as VARINT(2) (not sub-message)')
print('  Wire type 0 instead of 2 — field 1009 might be a direct int')
print('-'*60)
# Field 1009 varint tag = (1009 << 3) | 0 = 8072
# Encode: 8072 in varint = ?
# 8072 = 0x1F88
# 7-bit groups: 0001000 1111000 = 8, 63
# Wait: 8072 in binary = 1 1111 1000 1000
# Groups: 0001000 (=8) with continuation, 0111111 (=63) no continuation
# = 0x88 0x3F
# Actually: 8072 = 63*128 + 8? No: 63*128=8064, 8064+8=8072. Yes!
# Varint bytes: first byte = 8|(0x80) = 0x88, second = 63 = 0x3F
# So pb_varint(1009, 2) should work if our pb_varint handles it
pdata_1009_varint = pb_varint(6, ts()) + pb_varint(1009, 2)
acks = send_esg(pdata_1009_varint, label='f1009=2 varint')
obs = quick_watch(10)
tag = '[WORKED]' if obs['mode_changed'] else ('[OK]' if acks and any(a.get('action_id') == 1009 for a in acks) else '[REJ]')
print(f'  {tag} f1009=2(varint): acks={[a.get("action_id") for a in acks]} mode_chg={obs["mode_changed"]}')
results['test1_f1009_varint'] = {'acks': acks, 'obs': obs}
# Restore
send_esg(pb_varint(6, ts()) + pb_varint(1009, 1))
time.sleep(2)

# =====================================================================
# TEST 2: Scan f51-f80 on ESG ConfigWrite
# =====================================================================
print('\n' + '='*60)
print('TEST 2: ESG ConfigWrite f51-f80 (value=2)')
print('-'*60)
scan2 = {}
for fn in range(51, 81):
    acks = send_esg(pdata(pb_varint(fn, 2)), wait_s=4)
    accepted = acks and any(a.get('action_id') == fn for a in acks)
    m = state['mode']
    mode_chg = (m is not None and m != 1) if m is not None else False
    tag = '[WORKED]' if mode_chg else ('[OK]' if accepted else '.')
    scan2[fn] = {'accepted': accepted, 'mode_chg': mode_chg}
    if accepted or mode_chg:
        print(f'  {tag} f{fn}=2: acks={[a.get("action_id") for a in acks]} mode={m}')
        send_esg(pdata(pb_varint(fn, 0)), wait_s=2)
    time.sleep(0.5)
accepted51_80 = [fn for fn, v in scan2.items() if v['accepted']]
print(f'  Accepted in f51-f80: {accepted51_80}')

# =====================================================================
# TEST 3: ESG with cf=2/ci=87 (DPUX chgMaxSoc native format)
# The DPUX native command for chgMaxSoc uses cf=2/ci=87
# Maybe ESG has its own cf=2/ci=XX command for mode?
# =====================================================================
print('\n' + '='*60)
print('TEST 3: ESG with cf=2/ci=87 (DPUX native format on ESG topic)')
print('-'*60)
# DPUX chgMaxSoc: dest=2, cf=2, ci=87, pdata = 2-byte value
# Try sending similar to ESG with dest=11
pdata_v1 = struct.pack('<H', 2)   # 2-byte little-endian value = 2
acks = send_esg(pdata_v1, cf=2, ci=87, dest=11, ver=3, wait_s=5, label='cf2/ci87 dest11')
print(f'  cf=2/ci=87/dest=11: acks={acks}  mode={state["mode"]}')
time.sleep(3)

# Also try with dest=2 (DPU) but via ESG topic
acks2 = send_esg(pdata_v1, cf=2, ci=87, dest=2, ver=3, wait_s=5)
print(f'  cf=2/ci=87/dest=2:  acks={acks2}  mode={state["mode"]}')
time.sleep(3)

# =====================================================================
# TEST 4: Different cf values with ConfigWrite-like pdata on ESG
# Try cf=1, 3, 10, 64, 100 to find other command channels
# =====================================================================
print('\n' + '='*60)
print('TEST 4: ESG with various cf values (mode=2 pdata)')
print('-'*60)
for cf_try in [1, 2, 3, 10, 12, 20, 64, 100, 128, 200]:
    pd = pdata(pb_varint(5, 2))  # eps_mode_info=2 as test payload
    acks = send_esg(pd, cf=cf_try, ci=17, dest=11, ver=3, wait_s=4)
    m = state['mode']
    got = len(acks) > 0
    tag = '[REPLIED]' if got else '[silent]'
    print(f'  {tag} cf={cf_try}/ci=17: acks={[a.get("action_id") for a in acks]} mode={m}')
    if got:
        results[f'cf{cf_try}_ci17'] = {'acks': acks}
    time.sleep(1)

# =====================================================================
# TEST 5: ESG ConfigWrite dest=0 (broadcast destination)
# Try routing to dest=0 instead of dest=11
# =====================================================================
print('\n' + '='*60)
print('TEST 5: ESG ConfigWrite dest=0 (broadcast)')
print('-'*60)
pd = pdata(pb_varint(5, 2))
acks = send_esg(pd, cf=254, ci=17, dest=0, ver=3, wait_s=5)
print(f'  dest=0: acks={acks}  mode={state["mode"]}')
time.sleep(2)

# =====================================================================
# TEST 6: ESG ConfigWrite with accepted fields + NEW combinations
# Combine ALL known accepted fields in one command
# =====================================================================
print('\n' + '='*60)
print('TEST 6: ESG ConfigWrite ALL accepted fields simultaneously')
print('  f5=2, f7=0, f33=1, f35=2, f37=2, f47=2, f48=2, f49=2, f50=2')
print('  Maximum pressure on the ESG config — maybe combo triggers mode?')
print('-'*60)
pd = pdata(
    pb_varint(5, 2),   # eps_mode_info=2
    pb_varint(7, 0),   # charge_watt_power=0
    pb_varint(33, 1),  # cms_max_chg_soc=1 (extreme limit)
    pb_varint(35, 2),  # unknown=2
    pb_varint(37, 2),  # unknown=2
    pb_varint(47, 2),  # unknown=2
    pb_varint(48, 2),  # unknown=2
    pb_varint(49, 2),  # unknown=2
    pb_varint(50, 2),  # unknown=2
)
acks = send_esg(pd, wait_s=8)
print(f'  ACKs: {acks}')
# Watch for 20s
print('  Watching 20s...')
t0 = time.time()
while time.time() - t0 < 20:
    time.sleep(2)
    print(f'    [{int(time.time()-t0):2d}s] batt={f"{state["batt_w"]:.0f}W" if state["batt_w"] else "idle"}  mode={state["mode"]}')
# RESTORE
print('  Restoring...')
restore_pd = pdata(
    pb_varint(5, 0), pb_varint(7, 7200), pb_varint(33, 100),
    pb_varint(35, 0), pb_varint(37, 0), pb_varint(47, 0),
    pb_varint(48, 0), pb_varint(49, 0), pb_varint(50, 0)
)
acks_r = send_esg(restore_pd, wait_s=5)
print(f'  Restore ACKs: {acks_r}')
time.sleep(5)

# =====================================================================
# TEST 7: ESG ConfigWrite f1009 with different sub-message layouts
# =====================================================================
print('\n' + '='*60)
print('TEST 7: ESG ConfigWrite f1009 sub-message with mode=2 explicitly')
print('  Try different field numbers INSIDE the 1009 sub-message')
print('-'*60)
# From telemetry: f1009 sub-message has f4=mode. Maybe mode=2 needs a specific field.
# Try f1=2, f2=2, f3=2, f10=2 inside the sub-message
for inner_fn, inner_val in [(1, 2), (2, 2), (3, 2), (10, 2), (4, 0), (4, 3), (4, 10)]:
    submsg = pb_varint(inner_fn, inner_val)
    pd = pdata(pb_bytes(1009, submsg))
    acks = send_esg(pd, wait_s=4)
    m = state['mode']
    accepted = acks and len(acks[0].get('_all', [])) > 2  # more than just [1,2]
    tag = '[OK]' if accepted else '.'
    print(f'  {tag} f1009.f{inner_fn}={inner_val}: acks={[a.get("action_id") for a in acks]} mode={m}')
    time.sleep(1)

# =====================================================================
# TEST 8: JSON operateType variants on ESG topic
# Try different operateType values that might control mode
# =====================================================================
print('\n' + '='*60)
print('TEST 8: JSON operateType variants on ESG consumer topic')
print('-'*60)
n_esg = len(state['esg_replies'])
for op_type in ['acChgCfg', 'acDischgCfg', 'standbyTime', 'powerMode',
                 'setMode', 'modeChange', 'selfPowered']:
    payload_j = json.dumps({
        'from': 'HomeAssistant', 'id': '1', 'version': '1.1',
        'moduleType': 0, 'operateType': op_type,
        'params': {'mode': 2, 'smartBackupMode': 2}
    })
    client.publish(SET_ESG, payload_j, qos=1)
    time.sleep(2)

new_replies = state['esg_replies'][n_esg:]
print(f'  Replies after all JSON ops: {len(new_replies)}')
for r in new_replies:
    print(f'    {r}')
print(f'  Mode: {state["mode"]}')

# =====================================================================
# FINAL SUMMARY
# =====================================================================
print('\n' + '='*60)
print('FINAL SUMMARY')
print('='*60)
print(f'  Final mode: {state["mode"]}')
print(f'  Final batt: {f"{state["batt_w"]:.0f}W" if state["batt_w"] else "idle"}')
print(f'  Total ESG replies: {len(state["esg_replies"])}')
print(f'  Total DPU replies: {len(state["dpu_replies"])}')

print('\n  Scan results f51-f80:')
print(f'    Accepted: {accepted51_80}')

print('\n  Non-ConfigWrite cf tests (any replies):')
for k, v in results.items():
    if k.startswith('cf'):
        print(f'    {k}: {v}')

client.loop_stop()
client.disconnect()
