#!/usr/bin/env python3
"""
EcoFlow Command Test v2
========================
Part 1: Developer MQTT PD303_APP_SET commands
         - Connects TWO clients: consumer MQTT (telemetry) + developer MQTT (commands)
         - Correctly detects batt_w via f518 wire-type-2 float32 parse
         - Tests: chargeWattPower, ch1ForceCharge OFF/ON, smartBackupMode 2/0

Part 2: REST API signing variants for POST endpoint
         - Tries 5 different ways to sign the nested 'params' dict
         - Reports raw HTTP response for each so we can see which gets past 8521

NOTE: Run while battery is ACTIVELY CHARGING for best observability.
      batt_w = None means battery is idle (field 518 absent).
"""
import json, time, struct, hashlib, hmac, random, ssl
import urllib.request, urllib.error
import paho.mqtt.client as mqtt

# ─── Credentials ───────────────────────────────────────────────────────────────
creds = {}
with open('ecoflow_credentials.txt') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            creds[k.strip()] = v.strip()

CLIENT_ID  = creds['CLIENT_ID']
MQTT_USER  = creds['MQTT_USER']
MQTT_PASS  = creds['MQTT_PASS']
ACCESS_KEY = creds.get('ACCESS_KEY', '')
SECRET_KEY = creds.get('SECRET_KEY', '')
parts      = CLIENT_ID.split('_', 2)
USER_ID    = parts[2] if len(parts) >= 3 else parts[-1]
SN_ESG     = 'HR65ZA1AVH7J0027'
BASE_URL   = 'https://api.ecoflow.com'

print(f'USER_ID: {USER_ID}')
print(f'ACCESS_KEY: {ACCESS_KEY[:8]}...')

# ─── Consumer MQTT topics ──────────────────────────────────────────────────────
DATA_ESG = f'/app/device/property/{SN_ESG}'
GET_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/get'
REP_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply'

# ─── Protobuf parser (same as confirmed-working scripts) ──────────────────────
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
        else:
            break
    return fields

# ─── State ────────────────────────────────────────────────────────────────────
state = {
    'batt_w':  None,   # float, watts (+charge / -discharge) — ABSENT when idle
    'mode':    None,   # 1=backup/charging, 2=self-powered, None=idle
    'soc':     None,   # float, SOC %
    'con_rep': [],     # consumer MQTT set_reply messages
    'dev_rep': [],     # developer MQTT set_reply messages
}

def on_message_consumer(client, userdata, msg):
    try:
        p = msg.payload
        if msg.topic == REP_ESG:
            state['con_rep'].append(p)
            print(f'  CONSUMER_REPLY ({len(p)}b): {p[:80].hex()}')
            return
        # telemetry: outer→inner→pdata
        outer = parse_fields(p)
        inner_b = outer.get(1, b'')
        if not isinstance(inner_b, bytes): return
        inner = parse_fields(inner_b)
        pd_b  = inner.get(1, b'')
        if not isinstance(pd_b, bytes): return
        pdata = parse_fields(pd_b)

        # f518 = battery watts: wire type 2, 4 bytes packed float32
        if 518 in pdata and isinstance(pdata[518], bytes) and len(pdata[518]) == 4:
            state['batt_w'] = struct.unpack('<f', pdata[518])[0]

        # f1009 sub-message: mode (f4) and SOC (f5)
        if 1009 in pdata and isinstance(pdata[1009], bytes):
            sub = parse_fields(pdata[1009])
            if 5 in sub and isinstance(sub[5], bytes) and len(sub[5]) == 4:
                state['soc'] = round(struct.unpack('<f', sub[5])[0], 1)
            new_m = sub.get(4)
            if new_m != state['mode']:
                print(f'  *** MODE {state["mode"]} -> {new_m} ***')
                state['mode'] = new_m
    except:
        pass

def on_connect_consumer(client, userdata, flags, rc):
    print(f'Consumer MQTT rc={rc}')
    if rc == 0:
        client.subscribe(DATA_ESG, qos=0)   # MUST be qos=0 for telemetry
        client.subscribe(REP_ESG,  qos=1)

# Connect consumer MQTT
consumer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
consumer.on_connect = on_connect_consumer
consumer.on_message = on_message_consumer
consumer.username_pw_set(MQTT_USER, MQTT_PASS)
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
consumer.tls_set_context(ctx)
consumer.connect('mqtt.ecoflow.com', 8883, 60)
consumer.loop_start()
time.sleep(2)

# Trigger telemetry dump
consumer.publish(GET_ESG, json.dumps({
    'from': 'HomeAssistant', 'id': '9999', 'version': '1.1',
    'moduleType': 0, 'operateType': 'latestQuotas', 'params': {}
}), qos=1)

print('Waiting 12s for baseline telemetry...')
time.sleep(12)
bw = state['batt_w']
print(f'Baseline: batt={f"{bw:.0f}W" if bw is not None else "IDLE (f518 absent)"}  '
      f'mode={state["mode"]}  soc={state["soc"]}')
if bw is None:
    print('  NOTE: Battery is idle — batt_w delta tests will show None. Dev replies still visible.')

# ════════════════════════════════════════════════════════════════════════════════
# PART 1: DEVELOPER MQTT PD303_APP_SET COMMANDS
# ════════════════════════════════════════════════════════════════════════════════
print('\n' + '='*60)
print('PART 1: Developer MQTT commands (PD303_APP_SET)')
print('='*60)

# Get developer MQTT credentials via REST
print('Getting developer MQTT cert...')
nonce  = str(random.randint(10000, 999999))
ts_ms  = str(int(time.time() * 1000))
target = f'accessKey={ACCESS_KEY}&nonce={nonce}&timestamp={ts_ms}'
sig    = hmac.new(SECRET_KEY.encode(), target.encode(), hashlib.sha256).hexdigest()
headers = {'accessKey': ACCESS_KEY, 'nonce': nonce, 'timestamp': ts_ms, 'sign': sig}
req = urllib.request.Request(f'{BASE_URL}/iot-open/sign/certification', headers=headers)
cert = {}
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        cert = json.loads(r.read().decode())
    print(f'  Cert: code={cert.get("code")}  '
          f'msg={cert.get("message", cert.get("msg", ""))}')
except Exception as ex:
    print(f'  Cert request failed: {ex}')

if cert.get('code') == '0' and 'data' in cert:
    d        = cert['data']
    dev_host = d.get('url', 'mqtt.ecoflow.com')
    dev_port = int(d.get('port', 8883))
    dev_user = d.get('certificateAccount', '')
    dev_pass = d.get('certificatePassword', '')
    dev_set  = f'/open/{dev_user}/{SN_ESG}/set'
    dev_rep  = f'/open/{dev_user}/{SN_ESG}/set_reply'

    print(f'  Dev broker: {dev_host}:{dev_port}')
    print(f'  Dev user:   {dev_user}')
    print(f'  Set topic:  {dev_set}')
    print(f'  Rep topic:  {dev_rep}')

    def on_msg_dev(c, u, msg):
        state['dev_rep'].append(msg.payload)
        try:
            j = json.loads(msg.payload.decode())
            print(f'  DEV_REPLY on {msg.topic}:')
            print(f'    {json.dumps(j, indent=4)}')
        except Exception:
            print(f'  DEV_REPLY (bin) on {msg.topic}: {msg.payload[:100].hex()}')

    def on_connect_dev(c, u, f, rc):
        print(f'  Developer MQTT rc={rc}')
        if rc == 0:
            c.subscribe(dev_rep, qos=1)
            print(f'  Subscribed to {dev_rep}')

    try:
        dev_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION1,
            client_id=f'HOMEAUTO_{ACCESS_KEY[:8]}',
            protocol=mqtt.MQTTv311)
    except AttributeError:
        dev_client = mqtt.Client(client_id=f'HOMEAUTO_{ACCESS_KEY[:8]}', protocol=mqtt.MQTTv311)

    dev_client.username_pw_set(dev_user, dev_pass)
    ctx2 = ssl.create_default_context(); ctx2.check_hostname = False; ctx2.verify_mode = ssl.CERT_NONE
    dev_client.tls_set_context(ctx2)
    dev_client.on_connect = on_connect_dev
    dev_client.on_message = on_msg_dev
    dev_client.connect(dev_host, dev_port, keepalive=60)
    dev_client.loop_start()
    time.sleep(3)

    TESTS = [
        # Reduce charge rate first — most visible, safest test
        ('chargeWattPower=3000',    {'chargeWattPower': 3000}),
        # Kill charging
        ('ch1ForceCharge=OFF',      {'ch1ForceCharge': 'FORCE_CHARGE_OFF'}),
        # Resume charging
        ('ch1ForceCharge=ON',       {'ch1ForceCharge': 'FORCE_CHARGE_ON'}),
        # Switch to self-powered mode (should stop grid charging)
        ('smartBackupMode=2',       {'smartBackupMode': 2}),
        # Restore to backup mode
        ('smartBackupMode=0',       {'smartBackupMode': 0}),
    ]

    for label, params in TESTS:
        batt_before = state['batt_w']
        mode_before = state['mode']
        dev_n       = len(state['dev_rep'])
        seq         = str(int(time.time()))
        cmd = {
            'from':    'HomeAssistant',
            'id':      seq,
            'version': '1.0',
            'sn':      SN_ESG,
            'cmdCode': 'PD303_APP_SET',
            'params':  params,
        }
        pub_str = json.dumps(cmd)
        print(f'\n--- {label} ---')
        print(f'  payload: {pub_str}')
        rc = dev_client.publish(dev_set, pub_str, qos=1)
        print(f'  pub_rc={rc.rc}')

        for tick in range(15):
            time.sleep(1)
            cur   = state['batt_w']
            delta = round(cur - batt_before, 1) if (cur is not None and batt_before is not None) else None
            new_d = len(state['dev_rep']) - dev_n
            mark  = ''
            if state['mode'] != mode_before:
                mark = f'  *** MODE {mode_before}->{state["mode"]} CHANGED!'
            elif delta is not None and abs(delta) > 1500:
                mark = '  *** WORKED! (>1500W delta)'
            elif delta is not None and abs(delta) > 300:
                mark = '  ** significant (>300W)'
            elif delta is not None and abs(delta) > 50:
                mark = '  * noticeable (>50W)'
            if new_d:
                mark += f'  [dev_reply={new_d}]'
            if mark or tick % 5 == 4:
                bw_str = f'{cur:.0f}W' if cur is not None else 'idle'
                print(f'  [{tick+1:2d}s] batt={bw_str}  d={delta}W{mark}')

    dev_client.loop_stop()
    dev_client.disconnect()

else:
    print(f'  Developer cert failed, skipping Part 1.')
    print(f'  Response: {cert}')

# ════════════════════════════════════════════════════════════════════════════════
# PART 2: REST API SIGNING VARIANTS
# ════════════════════════════════════════════════════════════════════════════════
print('\n' + '='*60)
print('PART 2: REST API POST signing variants')
print(f'  ACCESS_KEY present: {bool(ACCESS_KEY)}')
print('='*60)

if not ACCESS_KEY:
    print('  No ACCESS_KEY — skipping REST tests.')
else:
    # Common POST body
    body_json = {'sn': SN_ESG, 'cmdCode': 'PD303_APP_SET', 'params': {'smartBackupMode': 2}}

    def do_post(variant_label, sign_dict, path, body_dict, method='POST'):
        nonce = str(random.randint(10000, 999999))
        ts_ms = str(int(time.time() * 1000))
        if sign_dict:
            sorted_str = '&'.join(f'{k}={v}' for k, v in sorted(sign_dict.items()))
            target = f'{sorted_str}&accessKey={ACCESS_KEY}&nonce={nonce}&timestamp={ts_ms}'
        else:
            target = f'accessKey={ACCESS_KEY}&nonce={nonce}&timestamp={ts_ms}'
        sig = hmac.new(SECRET_KEY.encode(), target.encode(), hashlib.sha256).hexdigest()
        h = {'accessKey': ACCESS_KEY, 'nonce': nonce, 'timestamp': ts_ms, 'sign': sig,
             'Content-Type': 'application/json;charset=UTF-8'}
        data = json.dumps(body_dict).encode()
        req = urllib.request.Request(f'{BASE_URL}{path}', data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            resp = {'http_error': e.code, 'body': e.read().decode()[:300]}
        except Exception as ex:
            resp = {'error': str(ex)}
        print(f'\n  {variant_label}')
        print(f'    sign_str: ...{sorted(sign_dict.items()) if sign_dict else "(empty)"}')
        print(f'    response: {resp}')
        return resp

    for endpoint in ['/iot-open/sign/device/quota/set', '/iot-open/sign/device/quota']:
        print(f'\n>>> Endpoint: {endpoint}')

        # A: exclude nested params entirely (known baseline)
        do_post('A: exclude nested params (baseline)',
                {'sn': SN_ESG, 'cmdCode': 'PD303_APP_SET'},
                endpoint, body_json)

        # B: include params as JSON string
        do_post('B: params as JSON string',
                {'sn': SN_ESG, 'cmdCode': 'PD303_APP_SET',
                 'params': json.dumps({'smartBackupMode': 2}, separators=(',', ':'))},
                endpoint, body_json)

        # C: dot-flatten nested params
        do_post('C: dot-flatten  params.smartBackupMode=2',
                {'sn': SN_ESG, 'cmdCode': 'PD303_APP_SET', 'params.smartBackupMode': '2'},
                endpoint, body_json)

        # D: sign empty dict (only accessKey+nonce+ts in sign string)
        do_post('D: sign nothing (empty sign_dict)',
                {},
                endpoint, body_json)

        # E: include sn only
        do_post('E: sign sn only',
                {'sn': SN_ESG},
                endpoint, body_json)

        # F: PUT method with variant A signing
        do_post('F: PUT method (sign A)',
                {'sn': SN_ESG, 'cmdCode': 'PD303_APP_SET'},
                endpoint, body_json, method='PUT')

    # Also try with chargeWattPower (int param, no nesting issue)
    print('\n>>> Also test: chargeWattPower (flat int, no nested dict)')
    body_flat = {'sn': SN_ESG, 'cmdCode': 'PD303_APP_SET', 'params': {'chargeWattPower': 3000}}
    do_post('G: chargeWattPower=3000, sign sn+cmdCode',
            {'sn': SN_ESG, 'cmdCode': 'PD303_APP_SET'},
            '/iot-open/sign/device/quota/set', body_flat)
    do_post('H: chargeWattPower=3000, sign sn+cmdCode+params(JSON)',
            {'sn': SN_ESG, 'cmdCode': 'PD303_APP_SET',
             'params': json.dumps({'chargeWattPower': 3000}, separators=(',', ':'))},
            '/iot-open/sign/device/quota/set', body_flat)

    # Watch 15s for any REST-triggered telemetry change
    print('\nWatching 15s for any REST-triggered effect...')
    t0 = time.time()
    bw0 = state['batt_w']; m0 = state['mode']
    while time.time() - t0 < 15:
        time.sleep(3)
        bw = state['batt_w']; m = state['mode']
        bw_str = f'{bw:.0f}W' if bw is not None else 'idle'
        print(f'  [{int(time.time()-t0):2d}s] batt={bw_str}  mode={m}')
        if m != m0:
            print(f'  *** MODE CHANGED: {m0} -> {m} ***')
            break

# ─── Final summary ─────────────────────────────────────────────────────────────
print('\n' + '='*60)
print('FINAL STATE')
print('='*60)
bw = state['batt_w']
print(f'  batt = {f"{bw:.0f}W" if bw is not None else "idle"}')
print(f'  mode = {state["mode"]}')
print(f'  soc  = {state["soc"]}')
print(f'  Consumer MQTT set_reply count: {len(state["con_rep"])}')
print(f'  Developer MQTT set_reply count: {len(state["dev_rep"])}')

consumer.loop_stop()
consumer.disconnect()
print('\nDone.')
