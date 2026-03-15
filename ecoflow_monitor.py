"""
ecoflow_monitor.py
Monitors ALL MQTT messages from both devices, decoding outer headers.
Shows cmd_func, cmd_id, src, dest for every message — helps understand
what message types the devices actually use, and if anything arrives after commands.
"""
import json, time, struct
import paho.mqtt.client as mqtt

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
SN_ESG    = 'HR65ZA1AVH7J0027'
SN_DPU    = 'P101ZA1A9HA70164'

# All subscriptions
TOPICS = [
    f'/app/device/property/{SN_ESG}',
    f'/app/device/property/{SN_DPU}',
    f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply',
    f'/app/{USER_ID}/{SN_DPU}/thing/property/set_reply',
    f'/app/{USER_ID}/{SN_ESG}/thing/property/get_reply',
    f'/app/{USER_ID}/{SN_DPU}/thing/property/get_reply',
    f'/app/{USER_ID}/+/thing/property/+',   # wildcard
]

GET_TOPIC_ESG = f'/app/{USER_ID}/{SN_ESG}/thing/property/get'
GET_TOPIC_DPU = f'/app/{USER_ID}/{SN_DPU}/thing/property/get'
SET_TOPIC_ESG = f'/app/{USER_ID}/{SN_ESG}/thing/property/set'

def pb_varint(v):
    r = b''
    while True:
        b = v & 0x7F; v >>= 7
        r += bytes([b | (0x80 if v else 0)])
        if not v: break
    return r
def pb_int(fn, v): return pb_varint((fn << 3) | 0) + pb_varint(v)
def pb_msg(fn, d): return pb_varint((fn << 3) | 2) + pb_varint(len(d)) + d
def pb_str(fn, s): b = s.encode(); return pb_varint((fn << 3) | 2) + pb_varint(len(b)) + b

def dvi(data, pos):
    r, s = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80): break
        s += 7
    return r, pos

def decode_flat(data, prefix='', out=None, depth=0):
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
                        out[key] = f'"{s2}"'; continue
                except: pass
                if depth < 3:  # only recurse 3 levels
                    decode_flat(raw, key + '.', out, depth+1)
            elif wt == 5:
                v = struct.unpack_from('<f', data, pos)[0]; pos += 4
                out[key] = f'f{round(v,2)}'
            else: break
        except: break
    return out

msg_count = [0]

def on_msg(c, u, msg):
    msg_count[0] += 1
    data = msg.payload

    # Try to decode as protobuf
    fields = decode_flat(data)

    # Show compact summary: topic, key header fields, selected data fields
    sn = 'ESG' if SN_ESG in msg.topic else 'DPU' if SN_DPU in msg.topic else '???'
    topic_short = msg.topic.split('/')[-1]

    # Extract header fields (level 1.x)
    cf   = fields.get('1.8', '?')   # cmd_func
    ci   = fields.get('1.9', '?')   # cmd_id
    src  = fields.get('1.2', '?')   # src
    dest = fields.get('1.3', '?')   # dest
    ver  = fields.get('1.16', '?')  # version

    # Try to decode as JSON
    json_str = ''
    try:
        j = json.loads(data)
        json_str = f' JSON:{list(j.keys())[:4]}'
    except: pass

    # Key data fields
    f518  = fields.get('1.1.518', '')
    f515  = fields.get('1.1.515', '')
    f1009 = {k:v for k,v in fields.items() if k.startswith('1.1.1009')}

    extra = ''
    if f518:  extra += f' batt={f518}'
    if f515:  extra += f' grid={f515}'
    if f1009: extra += f' {f1009}'
    if json_str: extra += json_str

    print(f'[{msg_count[0]:3d}] {sn}/{topic_short:10s} cf={cf:>3} ci={ci:>3} src={src:>3} dest={dest:>3} v={ver}{extra}')

def on_connect(c, u, f, rc):
    print(f'MQTT rc={rc}')
    if rc == 0:
        for t in TOPICS:
            c.subscribe(t, qos=1)
        print(f'Subscribed to all topics')

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
client.publish(GET_TOPIC_ESG, get_pl, qos=1)
client.publish(GET_TOPIC_DPU, get_pl, qos=1)
print('Watching for 30s (baseline), then sending a command, then 20s more...')
time.sleep(30)

print()
print('--- Sending stream ConfigWrite cmd_bms_power_off=1 to ESG ---')
pdata = pb_int(30, 1)   # cfg_bms_power_off = true
seq = int(time.time() * 1000) & 0xFFFFFFFF
header = (pb_msg(1,pdata)+pb_int(2,32)+pb_int(3,2)+pb_int(4,1)+pb_int(5,1)+
          pb_int(8,254)+pb_int(9,17)+pb_int(10,len(pdata))+pb_int(11,1)+
          pb_int(14,seq)+pb_int(15,56)+pb_int(16,3)+pb_int(17,1)+pb_str(23,'Android'))
payload = pb_msg(1, header)
rc = client.publish(SET_TOPIC_ESG, payload, qos=1)
print(f'Sent cmd rc={rc.rc}  hex: {payload.hex()}')
print()
time.sleep(20)

client.loop_stop()
client.disconnect()
print(f'\nTotal messages: {msg_count[0]}')
