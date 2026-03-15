#!/usr/bin/env python3
"""
ProtoPushAndSet Command Test
============================
Tests sending ProtoPushAndSet-format pdata on consumer MQTT with various
cf/ci combinations. This is the PD303 SHP2 protobuf message format.

ProtoPushAndSet key fields (from pd303_pb2 / ha-ef-ble reference):
  field  7: charge_watt_power     (uint32, watts)
  field 18: ch1_force_charge      (enum: 0=OFF, 1=ON)
  field 19: ch2_force_charge
  field 20: ch3_force_charge
  field 61: smart_backup_mode     (uint32: 0=none, 1=TOU, 2=self-powered, 3=sched)
  field  5: eps_mode_info         (bool)

BLE reference (ha-ef-ble shp2.py):
  SET commands use: Packet(src=0x21, target=0x0B, cmdSet=0x0C, cmdId=0x21, payload)
  RECEIVE data  :  packet.src==0x0B, cmdSet==0x0C, cmdId==0x01 (ProtoTime)
  RECEIVE data  :  packet.src==0x0B, cmdSet==0x0C, cmdId==0x20 (ProtoPushAndSet)

In decimal: cmdSet=12, cmdId=33 (send) | cmdId=32 (recv push) | cmdId=1 (ProtoTime)

Test matrix:
  pdata variants: ch1ForceCharge=ON, smart_backup_mode=2, chargeWattPower=1000
  cf variants:    12(PD303), 11, 13, 254
  ci variants:    33(0x21 send), 32(0x20 recv/push), 17(ConfigWrite), 21
"""
import json, time, struct, ssl
import paho.mqtt.client as mqtt

# ─── Credentials ───────────────────────────────────────────────────────────────
creds = {}
with open('ecoflow_credentials.txt') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            creds[k.strip()] = v.strip()

CLIENT_ID = creds['CLIENT_ID']
MQTT_USER = creds['MQTT_USER']
MQTT_PASS = creds['MQTT_PASS']
parts     = CLIENT_ID.split('_', 2)
USER_ID   = parts[2] if len(parts) >= 3 else parts[-1]
SN_ESG    = 'HR65ZA1AVH7J0027'

DATA_ESG = f'/app/device/property/{SN_ESG}'
GET_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/get'
SET_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set'
REP_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply'

# ─── Protobuf helpers ──────────────────────────────────────────────────────────
def pb_varint(field, value):
    tag = (field << 3) | 0
    result = b''
    v = tag
    while v > 0x7F: result += bytes([0x80 | (v & 0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    v = int(value)
    while v > 0x7F: result += bytes([0x80 | (v & 0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    return result

def pb_bytes(field, data):
    tag = (field << 3) | 2
    result = b''
    v = tag
    while v > 0x7F: result += bytes([0x80 | (v & 0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    v = len(data)
    while v > 0x7F: result += bytes([0x80 | (v & 0x7F)]); v >>= 7
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

def build_cmd(pdata_bytes, cf=254, ci=17, dest=11, ver=3):
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    hdr = (
        pb_bytes(1, pdata_bytes) + pb_varint(2, 32) + pb_varint(3, dest) +
        pb_varint(4, 1) + pb_varint(5, 1) + pb_varint(8, cf) + pb_varint(9, ci) +
        pb_varint(10, len(pdata_bytes)) + pb_varint(11, 1) + pb_varint(14, seq) +
        pb_varint(16, ver) + pb_varint(17, 1) + pb_string(23, 'Android')
    )
    return pb_bytes(1, hdr)

# ─── State ────────────────────────────────────────────────────────────────────
state = {'batt_w': None, 'mode': None, 'soc': None, 'replies': []}

def on_message(client, userdata, msg):
    try:
        p = msg.payload
        if msg.topic == REP_ESG:
            outer = parse_fields(p)
            inner = parse_fields(outer.get(1, b''))
            cf_v  = inner.get(8); ci_v = inner.get(9)
            pd_b  = inner.get(1, b'') if isinstance(inner.get(1), bytes) else b''
            pd    = parse_fields(pd_b)
            reply = {'cf': cf_v, 'ci': ci_v, 'fields': sorted(pd.keys()),
                     'f1': pd.get(1), 'f2': pd.get(2)}
            state['replies'].append(reply)
            print(f'  [REPLY] cf={cf_v}/ci={ci_v} pd_fields={sorted(pd.keys())} '
                  f'f1={pd.get(1)} f2={pd.get(2)} raw_pd={pd_b[:30].hex()}')
            return
        # Telemetry
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
            nm = sub.get(4)
            if nm != state['mode']:
                print(f'  *** MODE {state["mode"]} -> {nm} ***')
                state['mode'] = nm
    except: pass

def on_connect(c, u, f, rc):
    print(f'Consumer MQTT rc={rc}')
    if rc == 0:
        c.subscribe(DATA_ESG, qos=0)
        c.subscribe(REP_ESG, qos=1)

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
client.on_connect = on_connect
client.on_message = on_message
client.username_pw_set(MQTT_USER, MQTT_PASS)
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
client.tls_set_context(ctx)
client.connect('mqtt.ecoflow.com', 8883, 60)
client.loop_start()
time.sleep(2)

client.publish(GET_ESG, json.dumps({'from': 'HomeAssistant', 'id': '9999', 'version': '1.1',
                                    'moduleType': 0, 'operateType': 'latestQuotas', 'params': {}}), qos=1)
print('Baseline (10s)...')
time.sleep(10)
bw = state['batt_w']
print(f'Baseline: batt={f"{bw:.0f}W" if bw is not None else "IDLE"}  mode={state["mode"]}  soc={state["soc"]}')

print()
print('='*60)
print('TEST MATRIX: ProtoPushAndSet pdata x cf/ci combos')
print('  pdata A: ch1ForceCharge=ON  (f18=1)')
print('  pdata B: chargeWattPower=1000 + ch1ForceCharge=ON  (f7=1000, f18=1)')
print('  pdata C: smart_backup_mode=2  (f61=2)')
print('  pdata D: ch1ForceCharge=ON + smart_backup_mode=2  (f18=1, f61=2)')
print()
print('  cf/ci combos: (12,33) (12,32) (12,21) (12,17) (254,33) (254,32) (254,21) (11,33) (11,17)')
print('='*60)

# ProtoPushAndSet pdata variants
PDATA = {
    'A: ch1Force=ON':           pb_varint(18, 1),
    'B: chgWatt=1000+ch1=ON':  pb_varint(7, 1000) + pb_varint(18, 1),
    'C: smBackup=2':            pb_varint(61, 2),
    'D: ch1=ON+smBackup=2':    pb_varint(18, 1) + pb_varint(61, 2),
}

CF_CI = [
    (12, 33),   # BLE cmdSet=0x0C cmdId=0x21 (ProtoPushAndSet write)
    (12, 32),   # BLE cmdSet=0x0C cmdId=0x20 (ProtoPushAndSet push/recv)
    (12, 21),   # alternate
    (12, 17),   # ConfigWrite cf with PD303 module
    (254, 33),  # MQTT wrapper cf with ProtoPushAndSet cmdId
    (254, 32),  #
    (254, 21),  #
    (11, 33),   # dest value as cf (11=ESG)
    (11, 17),   #
]

for pdata_label, pdata_bytes in PDATA.items():
    print(f'\n--- pdata {pdata_label} (hex: {pdata_bytes.hex()}) ---')
    for cf, ci in CF_CI:
        n = len(state['replies'])
        batt_before = state['batt_w']
        mode_before = state['mode']

        payload = build_cmd(pdata_bytes, cf=cf, ci=ci)
        rc = client.publish(SET_ESG, payload, qos=1)
        time.sleep(5)

        new_r = state['replies'][n:]
        cur   = state['batt_w']
        delta = round(cur - batt_before, 1) if (cur is not None and batt_before is not None) else None
        mode_chg = state['mode'] != mode_before

        tag = '[REPLIED]' if new_r else '[silent]'
        mark = ''
        if mode_chg:   mark = f'  *** MODE {mode_before}->{state["mode"]}!'
        elif delta and abs(delta) > 200: mark = f'  *** batt_delta={delta}W!'

        print(f'  {tag} cf={cf}/ci={ci}: replies={[(r["f1"],r["f2"]) for r in new_r]}  '
              f'd={delta}W{mark}')

# ─── Also try: ConfigWrite f61 (smart_backup_mode field in ConfigWrite schema) ───
print()
print('='*60)
print('BONUS: ConfigWrite (cf=254/ci=17) with ProtoPushAndSet field numbers')
print('  (f61=2 = smart_backup_mode, f18=1 = ch1_force_charge, f7=1000 = chargeWattPower)')
print('='*60)

for label, pd in [
    ('ConfigWrite f61=2 (smart_backup_mode)', pb_varint(6, int(time.time())) + pb_varint(61, 2)),
    ('ConfigWrite f18=1 (ch1_force_charge)',  pb_varint(6, int(time.time())) + pb_varint(18, 1)),
    ('ConfigWrite f7=1000 (chargeWattPower)',  pb_varint(6, int(time.time())) + pb_varint(7, 1000)),
]:
    n = len(state['replies'])
    mode_before = state['mode']
    payload = build_cmd(pd, cf=254, ci=17)
    rc = client.publish(SET_ESG, payload, qos=1)
    time.sleep(6)
    new_r = state['replies'][n:]
    print(f'  {label}: replies={[(r["f1"],r["f2"],r.get("fields")) for r in new_r]}  mode={state["mode"]} (was {mode_before})')

# ─── Final ─────────────────────────────────────────────────────────────────────
print()
print('='*60)
print('FINAL STATE')
print('='*60)
bw = state['batt_w']
print(f'  batt={f"{bw:.0f}W" if bw is not None else "idle"}  mode={state["mode"]}  soc={state["soc"]}')
print(f'  Total MQTT replies: {len(state["replies"])}')

client.loop_stop()
client.disconnect()
print('Done.')
