"""
ecoflow_charge_v2.py
Tests ch1ForceCharge using the WORKING alternator pattern adapted for panel2.

From ef_alternator_data.js (a confirmed WORKING protobuf command device):
  muster = {
    header: {
      src: 32, dest: 20, dSrc: 1, dDest: 1,
      encType: 1, checkType: 3,
      cmdFunc: 254, cmdId: 17,    <-- SET cmd (vs 21=PUSH/telemetry)
      needAck: 1, seq: Date.now(),
      version: 19, payloadVer: 1,
      from: 'Android', deviceSn: serial,
      pdata: { ... }
    }
  }

Key new things NOT in v1:
  - dSrc=1, dDest=1 (fields 4, 5)
  - encType=1, checkType=3 (fields 6, 7)
  - from='Android' string (field 23 in setHeader)
  - deviceSn=SN string (field 25 in setHeader)
  - version=19 (not 4)
  - dest=20 (alternator-like) or dest=11 (device's reported src)
  - cmdFunc=254, cmdId=17  OR  cmdFunc=12, cmdId=33 (shp2cmd!)

ProtoPushAndSet: ch1_force_charge = field 18  (FORCE_CHARGE_ON=1, FORCE_CHARGE_OFF=0)
"""
import json, time, struct
import paho.mqtt.client as mqtt

# --- Credentials ---
creds = {}
for line in open('ecoflow_credentials.txt').read().splitlines():
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        creds[k.strip()] = v.strip()

MQTT_USER = creds['MQTT_USER']
MQTT_PASS = creds['MQTT_PASS']
CLIENT_ID = creds['CLIENT_ID']
_parts    = CLIENT_ID.split('_', 2)
USER_ID   = _parts[2] if len(_parts) >= 3 else _parts[-1]
SN        = 'HR65ZA1AVH7J0027'

DATA_TOPIC = f'/app/device/property/{SN}'
GET_TOPIC  = f'/app/{USER_ID}/{SN}/thing/property/get'
SET_TOPIC  = f'/app/{USER_ID}/{SN}/thing/property/set'
SET_REPLY  = f'/app/{USER_ID}/{SN}/thing/property/set_reply'

# --- Protobuf encoder ---
def pb_varint(v):
    r = b''
    while True:
        b = v & 0x7F; v >>= 7
        r += bytes([b | (0x80 if v else 0)])
        if not v: break
    return r

def pb_int(fn, v):   return pb_varint((fn << 3) | 0) + pb_varint(v)
def pb_msg(fn, d):   return pb_varint((fn << 3) | 2) + pb_varint(len(d)) + d
def pb_str(fn, s):   b = s.encode(); return pb_varint((fn << 3) | 2) + pb_varint(len(b)) + b

# --- Protobuf decoder ---
def dvi(data, pos):
    r, s = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80): break
        s += 7
    return r, pos

def decode_all(data, prefix='', out=None):
    if out is None: out = {}
    pos = 0
    while pos < len(data):
        try:
            tag, pos = dvi(data, pos)
            fn = tag >> 3; wt = tag & 7
            if fn == 0: break
            key = prefix + str(fn)
            if wt == 0:
                v, pos = dvi(data, pos)
                out[key] = v
            elif wt == 2:
                ln, pos = dvi(data, pos)
                raw = data[pos:pos+ln]; pos += ln
                try:
                    s2 = raw.decode('utf-8')
                    if s2.isprintable() and all(31 < ord(c) or c in ' \t' for c in s2):
                        out[key] = s2; continue
                except: pass
                decode_all(raw, key + '.', out)
            elif wt == 5:
                v = struct.unpack_from('<f', data, pos)[0]; pos += 4
                out[key] = round(v, 2)
            else: break
        except: break
    return out

# --- Build command using setMessage/setHeader pattern (like alternator) ---
# setHeader fields (from panel2 protoSource):
#   pdata=1, src=2, dest=3, d_src=4, d_dest=5,
#   enc_type=6, check_type=7, cmd_func=8, cmd_id=9,
#   data_len=10, need_ack=11, seq=14, version=16, payload_ver=17,
#   from=23, module_sn=24, device_sn=25
def build_cmd(pdata_bytes,
              src=32, dest=20, d_src=1, d_dest=1,
              enc_type=1, check_type=3,
              cmd_func=254, cmd_id=17,
              version=19, from_str='Android', device_sn=SN):
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    header = (
        pb_msg(1,  pdata_bytes)         +  # pdata (ProtoPushAndSet)
        pb_int(2,  src)                 +  # src
        pb_int(3,  dest)                +  # dest
        pb_int(4,  d_src)               +  # d_src
        pb_int(5,  d_dest)              +  # d_dest
        pb_int(6,  enc_type)            +  # enc_type
        pb_int(7,  check_type)          +  # check_type
        pb_int(8,  cmd_func)            +  # cmd_func
        pb_int(9,  cmd_id)              +  # cmd_id
        pb_int(10, len(pdata_bytes))    +  # data_len
        pb_int(11, 1)                   +  # need_ack
        pb_int(14, seq)                 +  # seq
        pb_int(16, version)             +  # version
        pb_int(17, 1)                   +  # payload_ver
        pb_str(23, from_str)            +  # from
        pb_str(25, device_sn)              # device_sn
    )
    return pb_msg(1, header)  # outer setMessage wrapper

def force_off(): return pb_int(18, 0)  # ch1_force_charge = FORCE_CHARGE_OFF
def force_on():  return pb_int(18, 1)  # ch1_force_charge = FORCE_CHARGE_ON

# --- MQTT state ---
batt_w  = [None]
msgs    = [0]
replies = []

def on_msg(c, u, msg):
    msgs[0] += 1
    if msg.topic == SET_REPLY:
        replies.append(msg.payload)
        try:
            print(f'  *** SET_REPLY (json): {msg.payload.decode()}')
        except:
            print(f'  *** SET_REPLY (hex):  {msg.payload[:120].hex()}')
            fields = decode_all(msg.payload)
            if fields: print(f'  *** SET_REPLY proto: {fields}')
        return
    fields = decode_all(msg.payload)
    for k, v in fields.items():
        if str(k) == '1.1.518' and isinstance(v, float):
            batt_w[0] = round(v, 1)

def on_connect(c, u, f, rc):
    print(f'MQTT rc={rc}')
    if rc == 0:
        c.subscribe(DATA_TOPIC, qos=1)
        c.subscribe(SET_REPLY,  qos=1)

try:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
except AttributeError:
    client = mqtt.Client(client_id=CLIENT_ID, protocol=mqtt.MQTTv311)

client.username_pw_set(MQTT_USER, MQTT_PASS)
client.tls_set()
client.on_connect = on_connect
client.on_message = on_msg
client.connect('mqtt.ecoflow.com', 8883, keepalive=60)
client.loop_start()
time.sleep(2)

# Trigger telemetry
get_pl = json.dumps({'from': 'HomeAssistant', 'id': '9999', 'version': '1.1',
                     'moduleType': 0, 'operateType': 'latestQuotas', 'params': {}})
client.publish(GET_TOPIC, get_pl, qos=1)
print('Waiting 12s for baseline...')
time.sleep(12)
print(f'Baseline: batt_w={batt_w[0]}W  msgs={msgs[0]}')
if batt_w[0] is None:
    print('WARNING: batt_w=None. Start charging from EcoFlow app for clear results.')

# ---- Test matrix ----
# All tests use the NEW alternator-style header with from='Android', deviceSn=SN
# The big new variables: enc_type/check_type, from, deviceSn, AND new cmd_func/cmd_id combos

TESTS = [
    # (label, pdata, dest, cmd_func, cmd_id, version, from_str)

    # Alternator pattern: cmdFunc=254, cmdId=17 (SET), dest=20, version=19
    ('FORCE_OFF 254/17 dest=20 v=19 Android', force_off(), 20, 254, 17, 19, 'Android'),
    ('FORCE_ON  254/17 dest=20 v=19 Android', force_on(),  20, 254, 17, 19, 'Android'),

    # shp2cmd: cmdFunc=12, cmdId=33 (mystery SHP2 cmd ID!), dest=20
    ('FORCE_OFF  12/33 dest=20 v=19 Android', force_off(), 20,  12, 33, 19, 'Android'),
    ('FORCE_ON   12/33 dest=20 v=19 Android', force_on(),  20,  12, 33, 19, 'Android'),

    # Try dest=11 (device's own src address in telemetry)
    ('FORCE_OFF 254/17 dest=11 v=19 Android', force_off(), 11, 254, 17, 19, 'Android'),
    ('FORCE_ON  254/17 dest=11 v=19 Android', force_on(),  11, 254, 17, 19, 'Android'),

    # shp2cmd with dest=11
    ('FORCE_OFF  12/33 dest=11 v=19 Android', force_off(), 11,  12, 33, 19, 'Android'),
    ('FORCE_ON   12/33 dest=11 v=19 Android', force_on(),  11,  12, 33, 19, 'Android'),

    # dest=32 (latestQuotas style) with alternator cmd ids
    ('FORCE_OFF 254/17 dest=32 v=19 Android', force_off(), 32, 254, 17, 19, 'Android'),
    ('FORCE_ON  254/17 dest=32 v=19 Android', force_on(),  32, 254, 17, 19, 'Android'),

    # Try 'iOS' from string (used in panel2 deviceCmd)
    ('FORCE_OFF  12/33 dest=20 v=19 iOS',    force_off(), 20,  12, 33, 19, 'iOS'),
    ('FORCE_ON   12/33 dest=20 v=19 iOS',    force_on(),  20,  12, 33, 19, 'iOS'),
]

print()
for label, pdata, dest, cf, ci, v, from_str in TESTS:
    batt_before = batt_w[0]
    reps_before = len(replies)
    payload = build_cmd(pdata, dest=dest, cmd_func=cf, cmd_id=ci, version=v, from_str=from_str)
    print(f'=== {label} ===')
    print(f'  hex({len(payload)}B): {payload.hex()}')
    rc = client.publish(SET_TOPIC, payload, qos=1)
    print(f'  pub rc={rc.rc}')
    for tick in range(12):
        time.sleep(1)
        cur   = batt_w[0]
        delta = round(cur - batt_before, 1) if (cur is not None and batt_before is not None) else None
        new_r = len(replies) - reps_before
        mk = ''
        if delta is not None and abs(delta) > 200: mk = '  *** WORKED!'
        elif delta is not None and abs(delta) > 80: mk = '  ** significant'
        if new_r: mk += f'  REPLY={new_r}'
        if mk or tick % 5 == 4:
            print(f'  [{tick+1:2d}s] batt={cur}W d={delta}W{mk}')
    print()

client.loop_stop()
client.disconnect()
print(f'Done. msgs={msgs[0]}, replies={len(replies)}')
