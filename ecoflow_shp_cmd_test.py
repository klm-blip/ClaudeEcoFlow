#!/usr/bin/env python3
"""
EcoFlow SHP JSON Command Test
==============================
Tests the ORIGINAL Smart Home Panel (SHP1) JSON command format on the HR65.

The SHP1 uses consumer MQTT with:
  operateType: "TCP"
  params.cmdSet: 11
  params.id: <command_id>

This format has NEVER been tried on the HR65 (SHP3). Since the HR65 is an SHP
family device, it may support backward-compatible SHP1 commands.

Commands from smart_home_panel.py reference (tolwi-hassio):
  id=17: Backup channel control (start/stop grid charging)
         sta=2, ctrlMode=1, ch=10 → enable AC1 backup charging
         sta=0, ctrlMode=0, ch=10 → disable AC1 backup charging
  id=29: Charge/discharge limits
         forceChargeHigh=<50-100>, discLower=<0-30>
  id=24: EPS mode toggle (eps=0/1)

Also tests developer MQTT with correct client_id format (Hassio-{certAccount}-EcoFlow)
and a GET trigger to /quota topic, per tolwi-hassio public_api.py.

Run with battery NOT actively charging to see if commands START charging.
Or run while charging to see if commands STOP charging.
"""
import json, time, struct, hashlib, hmac, random, ssl
import urllib.request, urllib.error
import paho.mqtt.client as mqtt

# ─── Credentials ──────────────────────────────────────────────────────────────
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

print(f'CLIENT_ID: {CLIENT_ID}')
print(f'USER_ID:   {USER_ID}')

# ─── Consumer MQTT topics ──────────────────────────────────────────────────────
DATA_ESG = f'/app/device/property/{SN_ESG}'
GET_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/get'
SET_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set'
REP_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply'

print(f'SET topic: {SET_ESG}')
print(f'REP topic: {REP_ESG}')

# ─── Protobuf state parser ─────────────────────────────────────────────────────
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

state = {'batt_w': None, 'mode': None, 'soc': None, 'replies': []}

def on_message_consumer(client, userdata, msg):
    try:
        p = msg.payload
        if msg.topic == REP_ESG:
            state['replies'].append(p)
            # Try JSON decode first
            try:
                j = json.loads(p.decode())
                print(f'\n  JSON REPLY on set_reply: {json.dumps(j)}')
            except Exception:
                print(f'\n  PROTO REPLY on set_reply ({len(p)}b): {p[:100].hex()}')
            return
        # Telemetry parse
        outer = parse_fields(p)
        inner_b = outer.get(1, b'')
        if not isinstance(inner_b, bytes): return
        inner = parse_fields(inner_b)
        pd_b  = inner.get(1, b'')
        if not isinstance(pd_b, bytes): return
        pdata = parse_fields(pd_b)
        if 518 in pdata and isinstance(pdata[518], bytes) and len(pdata[518]) == 4:
            state['batt_w'] = struct.unpack('<f', pdata[518])[0]
        if 1009 in pdata and isinstance(pdata[1009], bytes):
            sub = parse_fields(pdata[1009])
            if 5 in sub and isinstance(sub[5], bytes) and len(sub[5]) == 4:
                state['soc'] = round(struct.unpack('<f', sub[5])[0], 1)
            new_m = sub.get(4)
            if new_m != state['mode']:
                print(f'\n  *** MODE {state["mode"]} -> {new_m} ***')
                state['mode'] = new_m
    except: pass

def on_connect_consumer(client, userdata, flags, rc):
    print(f'Consumer MQTT rc={rc}')
    if rc == 0:
        client.subscribe(DATA_ESG, qos=0)
        client.subscribe(REP_ESG,  qos=1)
        print(f'  Subscribed to {DATA_ESG} (qos=0)')
        print(f'  Subscribed to {REP_ESG} (qos=1)')

consumer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
consumer.on_connect = on_connect_consumer
consumer.on_message = on_message_consumer
consumer.username_pw_set(MQTT_USER, MQTT_PASS)
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
consumer.tls_set_context(ctx)
consumer.connect('mqtt.ecoflow.com', 8883, 60)
consumer.loop_start()
time.sleep(2)

# Trigger telemetry
consumer.publish(GET_ESG, json.dumps({
    'from': 'HomeAssistant', 'id': '9999', 'version': '1.1',
    'moduleType': 0, 'operateType': 'latestQuotas', 'params': {}
}), qos=1)
print('Waiting 12s for baseline telemetry...')
time.sleep(12)

bw = state['batt_w']
print(f'\nBaseline: batt={f"{bw:.0f}W" if bw is not None else "IDLE"}  '
      f'mode={state["mode"]}  soc={state["soc"]}')

# ════════════════════════════════════════════════════════════════════════════════
# SHP1-FORMAT JSON TESTS on consumer MQTT SET topic
# ════════════════════════════════════════════════════════════════════════════════
print('\n' + '='*65)
print('SHP1-FORMAT JSON COMMANDS (operateType: TCP, cmdSet: 11)')
print('Target: consumer MQTT  ' + SET_ESG)
print('='*65)

def send_shp_cmd(label, shp_params, wait=18):
    """Send SHP1-format JSON command and watch for effect."""
    n_before  = len(state['replies'])
    bw_before = state['batt_w']
    m_before  = state['mode']
    seq       = str(int(time.time() * 1000) & 0xFFFFFFFF)

    payload = json.dumps({
        'from':        'HomeAssistant',
        'id':          seq,
        'version':     '1.0',
        'operateType': 'TCP',
        'params':      shp_params,
    })
    print(f'\n--- {label} ---')
    print(f'  payload: {payload}')
    rc = consumer.publish(SET_ESG, payload, qos=1)
    print(f'  pub_rc={rc.rc}')

    for tick in range(wait):
        time.sleep(1)
        bw    = state['batt_w']
        new_r = len(state['replies']) - n_before
        marks = []
        if state['mode'] != m_before:
            marks.append(f'*** MODE {m_before}->{state["mode"]} CHANGED!')
        if bw is not None and bw_before is not None and abs(bw - bw_before) > 300:
            marks.append(f'** LARGE WATT DELTA ({bw - bw_before:+.0f}W)')
        if new_r:
            marks.append(f'[{new_r} reply(s)]')
        if marks or tick % 5 == 4:
            bw_str = f'{bw:.0f}W' if bw is not None else 'idle'
            print(f'  [{tick+1:2d}s] batt={bw_str}  mode={state["mode"]}  {" ".join(marks)}')

# ── id=17: Backup channel control ─────────────────────────────────────────────
# From smart_home_panel.py: ch=10→AC1, ch=11→AC2
# sta=2/ctrlMode=1 = enable charging; sta=0/ctrlMode=0 = disable

send_shp_cmd('id=17 ch=10 sta=0 ctrlMode=0 (DISABLE AC1 charging)',
             {'cmdSet': 11, 'id': 17, 'sta': 0, 'ctrlMode': 0, 'ch': 10})

send_shp_cmd('id=17 ch=10 sta=2 ctrlMode=1 (ENABLE AC1 charging)',
             {'cmdSet': 11, 'id': 17, 'sta': 2, 'ctrlMode': 1, 'ch': 10})

send_shp_cmd('id=17 ch=11 sta=0 ctrlMode=0 (DISABLE AC2 charging)',
             {'cmdSet': 11, 'id': 17, 'sta': 0, 'ctrlMode': 0, 'ch': 11})

# ── id=29: Charge/discharge limits ────────────────────────────────────────────
send_shp_cmd('id=29 forceChargeHigh=85 discLower=10',
             {'cmdSet': 11, 'id': 29, 'forceChargeHigh': 85, 'discLower': 10})

# ── id=24: EPS mode ───────────────────────────────────────────────────────────
send_shp_cmd('id=24 eps=0 (disable EPS)',
             {'cmdSet': 11, 'id': 24, 'eps': 0})

# ── Also try with moduleType=0 wrapper (original SHP1 style) ──────────────────
print('\n--- id=17 ch=10 sta=0 WITH moduleType=0 at top level ---')
seq = str(int(time.time() * 1000) & 0xFFFFFFFF)
payload_mt = json.dumps({
    'from': 'HomeAssistant', 'id': seq, 'version': '1.0',
    'moduleType': 0,
    'operateType': 'TCP',
    'params': {'cmdSet': 11, 'id': 17, 'sta': 0, 'ctrlMode': 0, 'ch': 10},
})
print(f'  payload: {payload_mt}')
rc = consumer.publish(SET_ESG, payload_mt, qos=1)
print(f'  pub_rc={rc.rc}')
time.sleep(12)
bw = state['batt_w']
print(f'  After 12s: batt={f"{bw:.0f}W" if bw is not None else "idle"}  '
      f'mode={state["mode"]}  replies={len(state["replies"])}')

# ── Also try flat cmdCode=SHP3_APP_SET and ESG_APP_SET (guesses) ──────────────
print('\n' + '='*65)
print('BONUS: Try alternate cmdCode guesses on consumer MQTT')
print('='*65)
for code in ['HR65_APP_SET', 'SHP3_APP_SET', 'ESG_APP_SET', 'EMS_APP_SET',
             'SHP2_APP_SET', 'SMART_GATEWAY_APP_SET']:
    seq = str(int(time.time() * 1000) & 0xFFFFFFFF)
    cmd = json.dumps({
        'from': 'HomeAssistant', 'id': seq, 'version': '1.0',
        'sn': SN_ESG, 'cmdCode': code,
        'params': {'chargeWattPower': 3000},
    })
    n_before = len(state['replies'])
    rc = consumer.publish(SET_ESG, cmd, qos=1)
    time.sleep(6)
    new_r = len(state['replies']) - n_before
    print(f'  {code}: pub_rc={rc.rc}  replies={new_r}')

# ════════════════════════════════════════════════════════════════════════════════
# DEVELOPER MQTT: correct client_id + GET trigger (per public_api.py)
# ════════════════════════════════════════════════════════════════════════════════
print('\n' + '='*65)
print('DEVELOPER MQTT: correct client_id + GET trigger + SHP1 commands')
print('='*65)

if not ACCESS_KEY:
    print('  No ACCESS_KEY — skipping.')
else:
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
        print(f'  Cert: code={cert.get("code")}')
    except Exception as ex:
        print(f'  Cert request failed: {ex}')

    if cert.get('code') == '0' and 'data' in cert:
        d         = cert['data']
        dev_host  = d.get('url', 'mqtt.ecoflow.com')
        dev_port  = int(d.get('port', 8883))
        cert_acct = d.get('certificateAccount', '')
        cert_pass = d.get('certificatePassword', '')

        # Correct client_id per tolwi-hassio public_api.py line 38:
        # self.mqtt_info.client_id = f"Hassio-{self.mqtt_info.username}-{self.group}"
        dev_client_id = f'Hassio-{cert_acct}-EcoFlow'

        dev_set    = f'/open/{cert_acct}/{SN_ESG}/set'
        dev_rep    = f'/open/{cert_acct}/{SN_ESG}/set_reply'
        dev_quota  = f'/open/{cert_acct}/{SN_ESG}/quota'
        dev_get    = f'/open/{cert_acct}/{SN_ESG}/get'
        dev_status = f'/open/{cert_acct}/{SN_ESG}/status'

        print(f'  Dev broker:   {dev_host}:{dev_port}')
        print(f'  Cert account: {cert_acct}')
        print(f'  Client ID:    {dev_client_id}')
        print(f'  Topics:')
        print(f'    quota:  {dev_quota}')
        print(f'    set:    {dev_set}')
        print(f'    status: {dev_status}')

        dev_state = {'quota_msgs': 0, 'replies': []}

        def on_msg_dev(c, u, msg):
            try:
                j = json.loads(msg.payload.decode())
                if 'quota' in msg.topic:
                    dev_state['quota_msgs'] += 1
                    if dev_state['quota_msgs'] <= 3:
                        print(f'\n  DEV QUOTA ({msg.topic}): {json.dumps(j)[:200]}')
                    else:
                        print(f'\n  DEV QUOTA msg #{dev_state["quota_msgs"]}')
                elif 'set_reply' in msg.topic:
                    dev_state['replies'].append(j)
                    print(f'\n  DEV SET_REPLY: {json.dumps(j)}')
                elif 'status' in msg.topic:
                    print(f'\n  DEV STATUS: {json.dumps(j)}')
            except Exception:
                if 'set_reply' in msg.topic:
                    dev_state['replies'].append(msg.payload)
                    print(f'\n  DEV SET_REPLY (bin): {msg.payload[:80].hex()}')

        def on_connect_dev(c, u, f, rc):
            print(f'  Developer MQTT rc={rc}')
            if rc == 0:
                c.subscribe(dev_quota,  qos=0)
                c.subscribe(dev_rep,    qos=1)
                c.subscribe(dev_status, qos=1)
                print(f'  Subscribed to quota, set_reply, status')

        try:
            dev_cli = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION1,
                client_id=dev_client_id, protocol=mqtt.MQTTv311)
        except AttributeError:
            dev_cli = mqtt.Client(client_id=dev_client_id, protocol=mqtt.MQTTv311)
        ctx2 = ssl.create_default_context(); ctx2.check_hostname = False; ctx2.verify_mode = ssl.CERT_NONE
        dev_cli.tls_set_context(ctx2)
        dev_cli.username_pw_set(cert_acct, cert_pass)
        dev_cli.on_connect = on_connect_dev
        dev_cli.on_message = on_msg_dev
        dev_cli.connect(dev_host, dev_port, keepalive=60)
        dev_cli.loop_start()
        time.sleep(3)

        # Send GET trigger (even though public_api.py sets get_topic=None,
        # try it anyway — maybe HR65 needs it)
        get_trigger = json.dumps({
            'from': 'HomeAssistant', 'id': '8001',
            'version': '1.0', 'moduleType': 0,
            'operateType': 'latestQuotas', 'params': {}
        })
        print(f'\n  Sending GET trigger to {dev_get}...')
        dev_cli.publish(dev_get, get_trigger, qos=1)

        print('  Waiting 20s for dev quota telemetry...')
        time.sleep(20)
        print(f'  Dev quota msgs received: {dev_state["quota_msgs"]}')

        if dev_state['quota_msgs'] == 0:
            print('  No telemetry -- HR65 not active on developer MQTT topic.')
            print('  Trying SHP1 command via dev MQTT anyway...')

        # Try SHP1 command via developer MQTT
        seq = str(int(time.time()))
        shp_dev_cmd = json.dumps({
            'from':        'HomeAssistant',
            'id':          seq,
            'version':     '1.0',
            'operateType': 'TCP',
            'params':      {'cmdSet': 11, 'id': 17, 'sta': 0, 'ctrlMode': 0, 'ch': 10},
        })
        print(f'\n  DEV MQTT SHP1 cmd: {shp_dev_cmd}')
        rc = dev_cli.publish(dev_set, shp_dev_cmd, qos=1)
        print(f'  pub_rc={rc.rc}')
        time.sleep(12)
        print(f'  Dev replies: {len(dev_state["replies"])}')

        # Also try PD303_APP_SET via dev MQTT
        seq = str(int(time.time()))
        pd303_cmd = json.dumps({
            'from': 'HomeAssistant', 'id': seq, 'version': '1.0',
            'sn': SN_ESG, 'cmdCode': 'PD303_APP_SET',
            'params': {'chargeWattPower': 3000},
        })
        print(f'\n  DEV MQTT PD303 cmd: {pd303_cmd}')
        rc = dev_cli.publish(dev_set, pd303_cmd, qos=1)
        print(f'  pub_rc={rc.rc}')
        time.sleep(12)
        print(f'  Dev replies total: {len(dev_state["replies"])}')

        dev_cli.loop_stop()
        dev_cli.disconnect()
    else:
        print(f'  Cert failed: {cert}')

# ─── Summary ───────────────────────────────────────────────────────────────────
print('\n' + '='*65)
print('SUMMARY')
print('='*65)
bw = state['batt_w']
print(f'  Final state: batt={f"{bw:.0f}W" if bw is not None else "idle"}  '
      f'mode={state["mode"]}  soc={state["soc"]}')
print(f'  Consumer MQTT set_reply count: {len(state["replies"])}')
print()
if not state['replies']:
    print('  No replies → HR65 does NOT respond to SHP1 JSON commands on consumer MQTT.')
    print('  → Next step: mitmproxy to capture phone app REST calls.')
else:
    print(f'  Got {len(state["replies"])} reply(s) → some command format was understood!')

consumer.loop_stop()
consumer.disconnect()
print('\nDone.')
