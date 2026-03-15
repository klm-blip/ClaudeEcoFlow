"""
ecoflow_charge_onoff.py
Tests turning battery ch1 force-charge ON/OFF using the correct sentProtoPushAndSet structure.

Structure from ef_panel2_data.js protoSource:
  sentProtoPushAndSet { setHeader4 header = 1; }
  setHeader4 {
    ProtoPushAndSet pdata  = 1;
    int32 src              = 2;   <-- KEY: should be 32, not 2!
    int32 dest             = 3;
    int32 d_src            = 4;
    int32 d_dest           = 5;
    int32 cmd_func         = 8;
    int32 cmd_id           = 9;
    int32 data_len         = 10;
    int32 need_ack         = 11;
    int32 is_ack           = 12;
    int32 seq              = 14;
    int32 version          = 16;
    int32 payload_ver      = 17;
  }
  ProtoPushAndSet: ch1_force_charge = field 18
    FORCE_CHARGE_OFF = 0,  FORCE_CHARGE_ON = 1

All previous command attempts used src=2 — this is likely the root cause of failure.
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

print(f'USER_ID:   {USER_ID}')
print(f'SET_TOPIC: {SET_TOPIC}')
print(f'SET_REPLY: {SET_REPLY}')

# --- Minimal protobuf encoder ---
def pb_varint(v):
    r = b''
    while True:
        b = v & 0x7F; v >>= 7
        r += bytes([b | (0x80 if v else 0)])
        if not v: break
    return r

def pb_int(fn, v):
    """Encode an integer field (wire type 0 = varint)."""
    return pb_varint((fn << 3) | 0) + pb_varint(v)

def pb_msg(fn, data):
    """Encode an embedded message field (wire type 2 = length-delimited)."""
    return pb_varint((fn << 3) | 2) + pb_varint(len(data)) + data

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

# --- Build sentProtoPushAndSet command ---
def build_cmd(pdata_bytes, src=32, dest=32, cmd_func=12, cmd_id=32, version=4, need_ack=1):
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    header = (
        pb_msg(1,  pdata_bytes)       +  # ProtoPushAndSet pdata
        pb_int(2,  src)               +  # src = 32 (app/controller)
        pb_int(3,  dest)              +  # dest = 32 or 11
        pb_int(8,  cmd_func)          +  # cmd_func
        pb_int(9,  cmd_id)            +  # cmd_id
        pb_int(10, len(pdata_bytes))  +  # data_len
        pb_int(11, need_ack)          +  # need_ack = 1 (request reply)
        pb_int(14, seq)               +  # seq (timestamp ms)
        pb_int(16, version)           +  # version = 4 (matches device)
        pb_int(17, 1)                    # payload_ver
    )
    return pb_msg(1, header)  # sentProtoPushAndSet: field 1 = header

# ProtoPushAndSet pdata for ch1_force_charge (field 18)
def force_off(): return pb_int(18, 0)   # FORCE_CHARGE_OFF = 0
def force_on():  return pb_int(18, 1)   # FORCE_CHARGE_ON  = 1

# --- MQTT state ---
batt_w  = [None]
msgs    = [0]
replies = []

def on_msg(c, u, msg):
    msgs[0] += 1
    if msg.topic == SET_REPLY:
        replies.append(msg.payload)
        try:
            decoded = msg.payload.decode()
            print(f'  *** SET_REPLY (json): {decoded}')
        except:
            print(f'  *** SET_REPLY (hex):  {msg.payload[:120].hex()}')
            fields = decode_all(msg.payload)
            if fields:
                print(f'  *** SET_REPLY (proto): {fields}')
        return
    fields = decode_all(msg.payload)
    for k, v in fields.items():
        if str(k) == '1.1.518' and isinstance(v, float):
            batt_w[0] = round(v, 1)

def on_connect(c, u, f, rc):
    print(f'MQTT connect rc={rc}')
    if rc == 0:
        c.subscribe(DATA_TOPIC, qos=1)
        c.subscribe(SET_REPLY,  qos=1)
        print(f'  subscribed: telemetry + set_reply')

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

# Trigger telemetry with latestQuotas GET
get_pl = json.dumps({'from': 'HomeAssistant', 'id': '9999', 'version': '1.1',
                     'moduleType': 0, 'operateType': 'latestQuotas', 'params': {}})
client.publish(GET_TOPIC, get_pl, qos=1)
print('Waiting 12s for baseline telemetry...')
time.sleep(12)
print(f'Baseline: batt_w={batt_w[0]}W  msgs={msgs[0]}')

if batt_w[0] is None:
    print()
    print('NOTE: batt_w is None — battery not currently charging/discharging.')
    print('For best results, start charging from EcoFlow app first.')
    print('Continuing anyway (commands will still be tested)...')
    print()

# --- Test matrix ---
# Tests the ch1_force_charge field with key change: src=32 (not 2!)
# Also tests dest=11 (device address) and dest=32 (app address)
# And cmd_func/cmd_id variations
TESTS = [
    # (label, pdata, src, dest, cmd_func, cmd_id, version)

    # Most likely correct: src=32, dest=32, cf=12, ci=32
    ('ch1 FORCE_OFF  src=32 dest=32 cf=12 ci=32 v=4', force_off(), 32, 32, 12, 32, 4),
    ('ch1 FORCE_ON   src=32 dest=32 cf=12 ci=32 v=4', force_on(),  32, 32, 12, 32, 4),

    # Try dest=11 (the device's own src address in telemetry)
    ('ch1 FORCE_OFF  src=32 dest=11 cf=12 ci=32 v=4', force_off(), 32, 11, 12, 32, 4),
    ('ch1 FORCE_ON   src=32 dest=11 cf=12 ci=32 v=4', force_on(),  32, 11, 12, 32, 4),

    # Try cmd_func=0, cmd_id=0 (like latestQuotas uses no func/id)
    ('ch1 FORCE_OFF  src=32 dest=32 cf= 0 ci= 0 v=4', force_off(), 32, 32,  0,  0, 4),
    ('ch1 FORCE_ON   src=32 dest=32 cf= 0 ci= 0 v=4', force_on(),  32, 32,  0,  0, 4),

    # Try version=19 just in case
    ('ch1 FORCE_OFF  src=32 dest=32 cf=12 ci=32 v=19', force_off(), 32, 32, 12, 32, 19),
    ('ch1 FORCE_ON   src=32 dest=32 cf=12 ci=32 v=19', force_on(),  32, 32, 12, 32, 19),
]

print()
for label, pdata, src, dest, cf, ci, v in TESTS:
    batt_before = batt_w[0]
    reps_before = len(replies)
    payload = build_cmd(pdata, src=src, dest=dest, cmd_func=cf, cmd_id=ci, version=v)
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
print(f'Done. Total msgs={msgs[0]}, total replies={len(replies)}')
