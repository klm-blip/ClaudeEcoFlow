"""
ecoflow_charge_v3.py
Tests stream-family ConfigWrite command format on the ESG (Smart Gateway, HR65).

The device is the EcoFlow Smart Gateway (ESG), not the SHP2.
Field 518 (battery watts) appears in stream_ultra/stream_ac DisplayPropertyUpload.
The stream_ultra ConfigWrite command format (a working reference) uses:
  src=32, dest=2, dSrc=1, dDest=1,
  cmdFunc=254, cmdId=17,       <-- ConfigWrite SET command
  productId=56,                <-- NEW! product ID field
  version=3,                   <-- version=3, not 4 or 19!
  from='Android'               <-- Android, not iOS
  (no deviceSn in header)

ConfigWrite pdata fields to try for battery on/off:
  cfg_bms_power_off = 30 (bool) -- stop BMS/battery
  cfg_power_off = 3 (bool) -- stop everything
  Also trying on DPU X SN (P101ZA1A9HA70164)
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

SN_ESG  = 'HR65ZA1AVH7J0027'   # Smart Gateway (ESG) - main controller
SN_DPU  = 'P101ZA1A9HA70164'   # Delta Pro Ultra X (battery/inverter)

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

# --- Decoder ---
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

# --- Build stream ConfigWrite command ---
# Based on stream_ultra prepareProtoCmd:
#   src=32, dest=2, dSrc=1, dDest=1,
#   cmdFunc=254, cmdId=17 (ConfigWrite),
#   productId=56, version=3, payloadVer=1,
#   from='Android', needAck=1
def build_stream_cmd(pdata_bytes, dest=2, cmd_func=254, cmd_id=17,
                     product_id=56, version=3, from_str='Android'):
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    header = (
        pb_msg(1,  pdata_bytes)      +  # ConfigWrite pdata
        pb_int(2,  32)               +  # src = 32 (app)
        pb_int(3,  dest)             +  # dest
        pb_int(4,  1)                +  # d_src = 1
        pb_int(5,  1)                +  # d_dest = 1
        pb_int(8,  cmd_func)         +  # cmd_func
        pb_int(9,  cmd_id)           +  # cmd_id
        pb_int(10, len(pdata_bytes)) +  # data_len
        pb_int(11, 1)                +  # need_ack = 1
        pb_int(14, seq)              +  # seq
        pb_int(15, product_id)       +  # product_id  <-- KEY NEW FIELD!
        pb_int(16, version)          +  # version
        pb_int(17, 1)                +  # payload_ver
        pb_str(23, from_str)            # from
    )
    return pb_msg(1, header)

# pdata: ConfigWrite fields
# cfg_power_off = 3 (bool) -- power off the system
# cfg_power_on  = 4 (bool) -- power on the system
# cfg_bms_power_off = 30 (bool) -- power off BMS (stops battery)
# cms_max_chg_soc = 33 (uint32) -- max charge SOC (0 = don't charge)
# cms_min_dsg_soc = 34 (uint32) -- min discharge SOC (100 = don't discharge)
def pdata_bms_off():    return pb_int(30, 1)   # cfg_bms_power_off = true
def pdata_bms_on():     return pb_int(30, 0)   # cfg_bms_power_off = false
def pdata_pwr_off():    return pb_int(3,  1)   # cfg_power_off = true
def pdata_pwr_on():     return pb_int(4,  1)   # cfg_power_on = true
def pdata_no_chg():     return pb_int(33, 0)   # cms_max_chg_soc = 0 (no charging)
def pdata_chg_100():    return pb_int(33, 100) # cms_max_chg_soc = 100 (full charging)

# --- MQTT state ---
batt_w  = [None]
msgs    = [0]
replies = []

DATA_TOPIC_ESG = f'/app/device/property/{SN_ESG}'
DATA_TOPIC_DPU = f'/app/device/property/{SN_DPU}'
GET_TOPIC_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/get'
GET_TOPIC_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/get'
SET_TOPIC_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set'
SET_TOPIC_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/set'
REPLY_ESG      = f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply'
REPLY_DPU      = f'/app/{USER_ID}/{SN_DPU}/thing/property/set_reply'

print(f'USER_ID:      {USER_ID}')
print(f'SET_TOPIC_ESG: {SET_TOPIC_ESG}')
print(f'SET_TOPIC_DPU: {SET_TOPIC_DPU}')

def on_msg(c, u, msg):
    msgs[0] += 1
    if msg.topic in (REPLY_ESG, REPLY_DPU):
        replies.append((msg.topic, msg.payload))
        try:    print(f'  *** SET_REPLY ({msg.topic[-3:]}): {msg.payload.decode()}')
        except:
            print(f'  *** SET_REPLY ({msg.topic[-3:]} hex): {msg.payload[:80].hex()}')
            f = decode_all(msg.payload)
            if f: print(f'  *** proto: {f}')
        return
    fields = decode_all(msg.payload)
    for k, v in fields.items():
        if str(k) == '1.1.518' and isinstance(v, float):
            batt_w[0] = round(v, 1)

def on_connect(c, u, f, rc):
    print(f'MQTT rc={rc}')
    if rc == 0:
        for t in [DATA_TOPIC_ESG, DATA_TOPIC_DPU, REPLY_ESG, REPLY_DPU]:
            c.subscribe(t, qos=1)

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

# Trigger telemetry from ESG
get_pl = json.dumps({'from': 'HomeAssistant', 'id': '9999', 'version': '1.1',
                     'moduleType': 0, 'operateType': 'latestQuotas', 'params': {}})
client.publish(GET_TOPIC_ESG, get_pl, qos=1)
client.publish(GET_TOPIC_DPU, get_pl, qos=1)
print('Waiting 12s for baseline...')
time.sleep(12)
print(f'Baseline: batt_w={batt_w[0]}W  msgs={msgs[0]}')
if batt_w[0] is None:
    print('WARNING: batt_w=None. Start charging from EcoFlow app for clear results.')

# ---- Test matrix ----
# Key new params: productId=56, version=3, dest=2 (stream_ultra pattern)
TESTS = [
    # (label, pdata, set_topic, dest, cmd_func, cmd_id, product_id, version)

    # Stream ConfigWrite pattern on ESG: cfg_bms_power_off
    ('ESG bms_off 254/17 dest=2 pid=56 v=3',  pdata_bms_off(), SET_TOPIC_ESG, 2, 254, 17, 56, 3),
    ('ESG bms_on  254/17 dest=2 pid=56 v=3',  pdata_bms_on(),  SET_TOPIC_ESG, 2, 254, 17, 56, 3),

    # cfg_power_off/on on ESG
    ('ESG pwr_off 254/17 dest=2 pid=56 v=3',  pdata_pwr_off(), SET_TOPIC_ESG, 2, 254, 17, 56, 3),
    ('ESG pwr_on  254/17 dest=2 pid=56 v=3',  pdata_pwr_on(),  SET_TOPIC_ESG, 2, 254, 17, 56, 3),

    # Same on DPU X SN (battery is in DPU X)
    ('DPU bms_off 254/17 dest=2 pid=56 v=3',  pdata_bms_off(), SET_TOPIC_DPU, 2, 254, 17, 56, 3),
    ('DPU bms_on  254/17 dest=2 pid=56 v=3',  pdata_bms_on(),  SET_TOPIC_DPU, 2, 254, 17, 56, 3),

    # Try no productId, different version
    ('ESG bms_off 254/17 dest=2 pid=0  v=19', pdata_bms_off(), SET_TOPIC_ESG, 2, 254, 17,  0, 19),
    ('ESG bms_on  254/17 dest=2 pid=0  v=19', pdata_bms_on(),  SET_TOPIC_ESG, 2, 254, 17,  0, 19),

    # cms_max_chg_soc = 0 (stop charging) on ESG
    ('ESG max_chg_soc=0   dest=2 pid=56 v=3', pdata_no_chg(),  SET_TOPIC_ESG, 2, 254, 17, 56, 3),
    ('ESG max_chg_soc=100 dest=2 pid=56 v=3', pdata_chg_100(), SET_TOPIC_ESG, 2, 254, 17, 56, 3),
]

print()
for label, pdata, set_topic, dest, cf, ci, pid, v in TESTS:
    batt_before = batt_w[0]
    reps_before = len(replies)
    payload = build_stream_cmd(pdata, dest=dest, cmd_func=cf, cmd_id=ci,
                                product_id=pid, version=v)
    print(f'=== {label} ===')
    print(f'  hex({len(payload)}B): {payload.hex()}')
    rc = client.publish(set_topic, payload, qos=1)
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
