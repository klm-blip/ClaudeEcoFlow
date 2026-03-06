"""
Tests developer MQTT commands (PD303_APP_SET) while monitoring consumer MQTT for changes.
This dual-approach: commands via developer API channel, telemetry via consumer channel.
"""
import json
import time
import struct
import hashlib
import hmac
import random
import urllib.request
import paho.mqtt.client as mqtt

# Load credentials
creds = {}
for line in open('ecoflow_credentials.txt').read().splitlines():
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        creds[k.strip()] = v.strip()

MQTT_USER  = creds.get('MQTT_USER', '')
MQTT_PASS  = creds.get('MQTT_PASS', '')
CLIENT_ID  = creds.get('CLIENT_ID', '')
ACCESS_KEY = creds.get('ACCESS_KEY', '')
SECRET_KEY = creds.get('SECRET_KEY', '')
USER_ID    = CLIENT_ID.split('_', 2)[2]
SN         = 'HR65ZA1AVH7J0027'

# === Get developer cert ===
print('Getting developer MQTT cert...')
nonce  = str(random.randint(10000, 1000000))
ts     = str(int(time.time() * 1000))
target = f'accessKey={ACCESS_KEY}&nonce={nonce}&timestamp={ts}'
sign   = hmac.new(SECRET_KEY.encode(), target.encode(), hashlib.sha256).hexdigest()
headers = {'accessKey': ACCESS_KEY, 'nonce': nonce, 'timestamp': ts, 'sign': sign}
req  = urllib.request.Request('https://api.ecoflow.com/iot-open/sign/certification', headers=headers)
with urllib.request.urlopen(req, timeout=15) as r:
    cert = json.loads(r.read().decode())

if cert.get('code') != '0':
    print(f'Cert failed: {cert}')
    exit(1)

d        = cert['data']
dev_host = d.get('url', 'mqtt.ecoflow.com')
dev_port = int(d.get('port', 8883))
dev_user = d.get('certificateAccount', '')
dev_pass = d.get('certificatePassword', '')
dev_set  = f'/open/{dev_user}/{SN}/set'
dev_rep  = f'/open/{dev_user}/{SN}/set_reply'
print(f'Dev user: {dev_user}')
print(f'Dev set:  {dev_set}')


# === Protobuf decoder ===
def dvi(data, pos):
    r, s = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80): break
        s += 7
    return r, pos

def decode_all(data, prefix='', out=None):
    if out is None:
        out = {}
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
                        out[key] = s2
                        continue
                except Exception:
                    pass
                decode_all(raw, key + '.', out)
            elif wt == 5:
                v = struct.unpack_from('<f', data, pos)[0]; pos += 4
                out[key] = round(v, 2)
            else:
                break
        except Exception:
            break
    return out


# === Consumer MQTT (telemetry monitor) ===
batt_w  = [None]
msgs    = [0]
replies_consumer = []
replies_dev      = []

data_topic  = f'/app/device/property/{SN}'
get_topic   = f'/app/{USER_ID}/{SN}/thing/property/get'
set_reply_c = f'/app/{USER_ID}/{SN}/thing/property/set_reply'


def on_msg_consumer(c, u, msg):
    msgs[0] += 1
    if msg.topic == set_reply_c:
        replies_consumer.append(msg.payload)
        try:
            print(f'  CONSUMER_REPLY: {msg.payload.decode()}')
        except Exception:
            print(f'  CONSUMER_REPLY (bin): {msg.payload[:100].hex()}')
        return
    fields = decode_all(msg.payload)
    for k, v in fields.items():
        if str(k) == '1.1.518' and isinstance(v, float):
            batt_w[0] = round(v, 1)


def on_connect_consumer(c, u, f, rc):
    print(f'Consumer MQTT rc={rc}')
    if rc == 0:
        c.subscribe(data_topic, qos=1)
        c.subscribe(set_reply_c, qos=1)


try:
    consumer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
except AttributeError:
    consumer = mqtt.Client(client_id=CLIENT_ID, protocol=mqtt.MQTTv311)

consumer.username_pw_set(MQTT_USER, MQTT_PASS)
consumer.tls_set()
consumer.on_connect = on_connect_consumer
consumer.on_message = on_msg_consumer
consumer.connect('mqtt.ecoflow.com', 8883, keepalive=60)
consumer.loop_start()
time.sleep(2)

# Trigger telemetry
get_pl = json.dumps({'from': 'HomeAssistant', 'id': '9999', 'version': '1.1',
                     'moduleType': 0, 'operateType': 'latestQuotas', 'params': {}})
consumer.publish(get_topic, get_pl, qos=1)
print('Baseline (12s)...')
time.sleep(12)
print(f'Baseline: batt_w={batt_w[0]}W  msgs={msgs[0]}')
if batt_w[0] is None:
    print('WARNING: No f518 telemetry. Battery may be idle (not charging/discharging).')


# === Developer MQTT (command channel) ===
def on_msg_dev(c, u, msg):
    replies_dev.append(msg.payload)
    try:
        print(f'  DEV_REPLY on {msg.topic}: {msg.payload.decode()}')
    except Exception:
        print(f'  DEV_REPLY (bin): {msg.payload[:100].hex()}')


def on_connect_dev(c, u, f, rc):
    print(f'Developer MQTT rc={rc}')
    if rc == 0:
        c.subscribe(dev_rep, qos=1)
        print(f'  subscribed: {dev_rep}')


try:
    dev_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                             client_id=f'HOMEAUTO_{ACCESS_KEY[:8]}',
                             protocol=mqtt.MQTTv311)
except AttributeError:
    dev_client = mqtt.Client(client_id=f'HOMEAUTO_{ACCESS_KEY[:8]}', protocol=mqtt.MQTTv311)

dev_client.username_pw_set(dev_user, dev_pass)
dev_client.tls_set()
dev_client.on_connect = on_connect_dev
dev_client.on_message = on_msg_dev
dev_client.connect(dev_host, dev_port, keepalive=60)
dev_client.loop_start()
time.sleep(3)

# === Tests ===
# Focus on START/STOP charge and mode switch as user requested
TESTS = [
    # Reduce charge rate - most visible test
    ('DEV chargeWattPower=3000', {'sn': SN, 'cmdCode': 'PD303_APP_SET', 'params': {'chargeWattPower': 3000}}),
    # Force charge OFF (stop charging)
    ('DEV ch1ForceCharge=OFF',   {'sn': SN, 'cmdCode': 'PD303_APP_SET', 'params': {'ch1ForceCharge': 'FORCE_CHARGE_OFF'}}),
    # Force charge ON (resume charging)
    ('DEV ch1ForceCharge=ON',    {'sn': SN, 'cmdCode': 'PD303_APP_SET', 'params': {'ch1ForceCharge': 'FORCE_CHARGE_ON'}}),
    # Mode switch to self-powered (smartBackupMode=2 or epsModeInfo?)
    ('DEV smartBackupMode=2',    {'sn': SN, 'cmdCode': 'PD303_APP_SET', 'params': {'smartBackupMode': 2}}),
    # Restore to normal backup mode
    ('DEV smartBackupMode=0',    {'sn': SN, 'cmdCode': 'PD303_APP_SET', 'params': {'smartBackupMode': 0}}),
]

print()
for label, cmd_body in TESTS:
    batt_before  = batt_w[0]
    dev_reps_b   = len(replies_dev)
    con_reps_b   = len(replies_consumer)
    seq          = str(int(time.time()))
    payload      = {'from': 'HomeAssistant', 'id': seq, 'version': '1.0'}
    payload.update(cmd_body)
    pub_str = json.dumps(payload)
    print(f'=== {label} ===')
    print(f'  {pub_str[:120]}')
    rc = dev_client.publish(dev_set, pub_str, qos=1)
    print(f'  pub rc={rc.rc}')

    for tick in range(15):
        time.sleep(1)
        cur   = batt_w[0]
        delta = round(cur - batt_before, 1) if (cur is not None and batt_before is not None) else None
        new_d = len(replies_dev) - dev_reps_b
        new_c = len(replies_consumer) - con_reps_b
        mk    = ''
        if delta and abs(delta) > 2000:
            mk = '  *** WORKED!'
        elif delta and abs(delta) > 500:
            mk = '  ** significant'
        elif delta and abs(delta) > 100:
            mk = '  * noticeable'
        if new_d or new_c:
            mk += f'  dev_reply={new_d} con_reply={new_c}'
        if mk or tick % 5 == 4:
            print(f'  [{tick+1:2d}s] batt={cur}W d={delta}W{mk}')
    print()

consumer.loop_stop()
consumer.disconnect()
dev_client.loop_stop()
dev_client.disconnect()
print('Done.')
