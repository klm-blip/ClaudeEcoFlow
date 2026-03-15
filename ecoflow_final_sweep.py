#!/usr/bin/env python3
"""
Final sweep: adjacent ci values + developer REST API SET commands.
Tests while charging is active.
"""
import ssl, json, time, struct, hashlib, hmac, random
import urllib.request, urllib.parse, urllib.error
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
ACCESS_KEY = creds.get('ACCESS_KEY', '')
SECRET_KEY = creds.get('SECRET_KEY', '')
parts      = CLIENT_ID.split('_', 2)
USER_ID    = parts[2] if len(parts) >= 3 else parts[-1]
SN_ESG = 'HR65ZA1AVH7J0027'
SN_DPU = 'P101ZA1A9HA70164'

SET_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set'
DATA_ESG = f'/app/device/property/{SN_ESG}'
REP_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply'
GET_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/get'

def pb_varint(field, value):
    tag = (field << 3) | 0; result = b''
    v = tag
    while v > 0x7F: result += bytes([0x80|(v&0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    v = int(value)
    while v > 0x7F: result += bytes([0x80|(v&0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    return result

def pb_bytes(field, data):
    tag = (field << 3) | 2; result = b''
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
        elif wt == 5: fields[fn] = payload[i:i+4]; i += 4
        else: break
    return fields

def build_esg(pdata_bytes, cf=254, ci=17, dest=11, ver=3):
    seq = int(time.time()*1000) & 0xFFFFFFFF
    hdr = (
        pb_bytes(1, pdata_bytes) + pb_varint(2, 32) + pb_varint(3, dest) +
        pb_varint(4, 1) + pb_varint(5, 1) + pb_varint(8, cf) + pb_varint(9, ci) +
        pb_varint(10, len(pdata_bytes)) + pb_varint(11, 1) + pb_varint(14, seq) +
        pb_varint(16, ver) + pb_varint(17, 1) + pb_string(23, 'Android')
    )
    return pb_bytes(1, hdr)

state = {'batt_w': None, 'mode': None, 'soc': None, 'replies': []}

def on_message(client, userdata, msg):
    try:
        t = msg.topic; p = msg.payload
        outer = parse_fields(p); inner = parse_fields(outer.get(1, b''))
        cf_v = inner.get(8); ci_v = inner.get(9)
        pd_b = inner.get(1, b'') if isinstance(inner.get(1), bytes) else b''
        if t == REP_ESG and ci_v != 20:
            pd = parse_fields(pd_b)
            state['replies'].append({'cf': cf_v, 'ci': ci_v, 'action_id': pd.get(1),
                                     'ok': pd.get(2), 'fields': sorted(pd.keys())})
            print(f'  [ACK] cf={cf_v}/ci={ci_v} action_id={pd.get(1)} ok={pd.get(2)}')
        elif t == DATA_ESG and pd_b:
            pdata = parse_fields(pd_b)
            if 518 in pdata and isinstance(pdata[518], bytes) and len(pdata[518]) == 4:
                state['batt_w'] = struct.unpack('<f', pdata[518])[0]
            if 1009 in pdata and isinstance(pdata[1009], bytes):
                sub = parse_fields(pdata[1009])
                if 5 in sub and isinstance(sub[5], bytes) and len(sub[5]) == 4:
                    state['soc'] = struct.unpack('<f', sub[5])[0]
                old_m = state['mode']; new_m = sub.get(4)
                if new_m != old_m:
                    state['mode'] = new_m
                    print(f'  *** MODE {old_m} -> {new_m} ***')
    except: pass

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe([(DATA_ESG, 0), (REP_ESG, 0)])
        gp = json.dumps({'from': 'HomeAssistant', 'id': '1', 'version': '1.1',
                         'moduleType': 0, 'operateType': 'latestQuotas', 'params': {}})
        client.publish(GET_ESG, gp, qos=1)
        print('Connected')
    else:
        print(f'Connect failed rc={rc}')

client_m = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
client_m.on_connect = on_connect
client_m.on_message = on_message
client_m.username_pw_set(MQTT_USER, MQTT_PASS)
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
client_m.tls_set_context(ctx)
client_m.connect('mqtt.ecoflow.com', 8883, 60)
client_m.loop_start()
time.sleep(10)

bw = state['batt_w']
print(f'State: batt={f"{bw:.0f}W" if bw else "idle"}  mode={state["mode"]}  soc={state["soc"]}')

def ts(): return int(time.time())
def pdata_f5_2(): return pb_varint(6, ts()) + pb_varint(5, 2)

# ================================================================
# PART 1: Adjacent ci values (ci=16, 19, 21, 32, 33, 34, 64)
# ================================================================
print('\n' + '='*55)
print('PART 1: Adjacent ci values (cf=254, various ci)')
print('='*55)

for ci_try in [16, 19, 21, 32, 33, 34, 64, 128]:
    n = len(state['replies'])
    client_m.publish(SET_ESG, build_esg(pdata_f5_2(), cf=254, ci=ci_try), qos=1)
    time.sleep(4)
    new = state['replies'][n:]
    m = state['mode']
    tag = '[REPLIED]' if new else '[silent]'
    print(f'  {tag} cf=254/ci={ci_try}: '
          f'replies={[(r["action_id"], r["ci"]) for r in new]}  mode={m}')

# ================================================================
# PART 2: Developer REST API SET commands
# ================================================================
print('\n' + '='*55)
print('PART 2: Developer REST API SET commands')
print(f'  ACCESS_KEY present: {len(ACCESS_KEY) > 0}')
print('='*55)

BASE_URL = 'https://api.ecoflow.com'

def rest_sign(body_dict):
    nonce = str(random.randint(10000, 999999))
    ts_ms = str(int(time.time() * 1000))
    flat = {k: str(v) for k, v in body_dict.items() if not isinstance(v, dict)}
    if flat:
        sorted_str = '&'.join(f'{k}={v}' for k, v in sorted(flat.items()))
        target = f'{sorted_str}&accessKey={ACCESS_KEY}&nonce={nonce}&timestamp={ts_ms}'
    else:
        target = f'accessKey={ACCESS_KEY}&nonce={nonce}&timestamp={ts_ms}'
    sig = hmac.new(SECRET_KEY.encode(), target.encode(), hashlib.sha256).hexdigest()
    return {'accessKey': ACCESS_KEY, 'nonce': nonce, 'timestamp': ts_ms, 'sign': sig,
            'Content-Type': 'application/json;charset=UTF-8'}

def rest_post(path, body):
    if not ACCESS_KEY:
        return {'error': 'no ACCESS_KEY in credentials'}
    headers = rest_sign(body)
    data = json.dumps(body).encode()
    req = urllib.request.Request(f'{BASE_URL}{path}', data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {'http_error': e.code, 'body': e.read().decode()[:200]}
    except Exception as ex:
        return {'error': str(ex)}

# Test candidate REST endpoints for SET commands
for path in ['/iot-open/sign/device/quota', '/iot-open/sign/device/quota/set',
             '/iot-open/sign/device/cmd']:
    body = {'sn': SN_ESG, 'cmdCode': 'PD303_APP_SET',
            'params': {'smartBackupMode': 2}}
    r = rest_post(path, body)
    print(f'\n  POST {path}')
    print(f'  body: {json.dumps(body)}')
    print(f'  resp: {r}')

# Watch 20s
print('\nWatching telemetry 20s for any REST-triggered effect...')
t0 = time.time()
batt_start = state['batt_w']; mode_start = state['mode']
while time.time() - t0 < 20:
    time.sleep(2)
    bw = state['batt_w']; m = state['mode']
    print(f'  [{int(time.time()-t0):2d}s] batt={f"{bw:.0f}W" if bw else "idle"}  mode={m}')
    if m != mode_start:
        print(f'  *** MODE CHANGED: {mode_start} -> {m} ***')
        break
print(f'  batt: {batt_start} -> {state["batt_w"]}  mode: {mode_start} -> {state["mode"]}')

# Also try chargeWattPower via REST
print('\n  Trying chargeWattPower REST commands:')
for params in [{'chargeWattPower': 100}, {'chargeWattPower': 7200}]:
    body = {'sn': SN_ESG, 'cmdCode': 'PD303_APP_SET', 'params': params}
    r = rest_post('/iot-open/sign/device/quota', body)
    print(f'  {params}: {r}')
    time.sleep(2)

# ================================================================
# FINAL
# ================================================================
print('\n' + '='*55)
print('FINAL STATE')
print('='*55)
bw = state['batt_w']
print(f'  batt={f"{bw:.0f}W" if bw else "idle"}  mode={state["mode"]}  soc={state["soc"]}')
print(f'  Total MQTT replies: {len(state["replies"])}')

client_m.loop_stop()
client_m.disconnect()
