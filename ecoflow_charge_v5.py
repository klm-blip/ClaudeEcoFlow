#!/usr/bin/env python3
"""
EcoFlow charge control v5 - decode ACK values and test cfg_bms_power_off.
We confirmed cfgUtcTime was the missing key in v4.
Now we try to actually STOP charging on the DPU.
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

SET_TOPIC_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/set'
SET_TOPIC_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set'
DATA_TOPIC_ESG = f'/app/device/property/{SN_ESG}'
DATA_TOPIC_DPU = f'/app/device/property/{SN_DPU}'

# ── Protobuf helpers ────────────────────────────────────────────
def pb_varint(field, value):
    tag = (field << 3) | 0
    result = b''
    v = tag
    while v > 0x7F: result += bytes([0x80|(v&0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    v = value
    while v > 0x7F: result += bytes([0x80|(v&0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    return result

def pb_string(field, value):
    tag = (field << 3) | 2
    result = b''
    v = tag
    while v > 0x7F: result += bytes([0x80|(v&0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    enc = value.encode('utf-8')
    length = len(enc)
    while length > 0x7F: result += bytes([0x80|(length&0x7F)]); length >>= 7
    result += bytes([length & 0x7F])
    return result + enc

def pb_bytes(field, value):
    tag = (field << 3) | 2
    result = b''
    v = tag
    while v > 0x7F: result += bytes([0x80|(v&0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    length = len(value)
    while length > 0x7F: result += bytes([0x80|(length&0x7F)]); length >>= 7
    result += bytes([length & 0x7F])
    return result + value

def parse_fields(payload):
    """Parse protobuf fields: returns dict of {field_num: value}."""
    fields = {}
    i = 0
    while i < len(payload):
        if payload[i] == 0: break
        tag = 0; shift = 0
        while i < len(payload):
            b = payload[i]; i += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80): break
            shift += 7
        field_num = tag >> 3
        wire_type = tag & 7
        if wire_type == 0:   # varint
            val = 0; shift = 0
            while i < len(payload):
                b = payload[i]; i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            fields[field_num] = val
        elif wire_type == 2: # length-delimited
            length = 0; shift = 0
            while i < len(payload):
                b = payload[i]; i += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            fields[field_num] = payload[i:i+length]
            i += length
        elif wire_type == 5: # 32-bit
            fields[field_num] = payload[i:i+4]
            i += 4
        else: break
    return fields

def build_dpu_cmd(pdata_bytes):
    """Build ConfigWrite for DPU: dest=2, cmdFunc=254, cmdId=17, version=3."""
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    header = (
        pb_bytes(1,  pdata_bytes) +
        pb_varint(2, 32) +   # src=32 (app)
        pb_varint(3, 2)  +   # dest=2 (DPU)
        pb_varint(4, 1)  +   # d_src=1
        pb_varint(5, 1)  +   # d_dest=1
        pb_varint(8, 254)+   # cmd_func=254
        pb_varint(9, 17) +   # cmd_id=17 (ConfigWrite)
        pb_varint(10, len(pdata_bytes)) +
        pb_varint(11, 1) +   # need_ack=1
        pb_varint(14, seq) +
        pb_varint(16, 3) +   # version=3
        pb_varint(17, 1) +   # payload_ver=1
        pb_string(23, 'Android')
    )
    return pb_bytes(1, header)

# ── State ────────────────────────────────────────────────────────
state = {'batt_w': None, 'soc': None, 'msg_count': 0}
ack_values = []

def decode_ack(pdata_bytes):
    """Decode ConfigWriteAck fields fully."""
    p = parse_fields(pdata_bytes)
    result = {}
    # ConfigWriteAck: field 1=action_id, field 2=config_ok, field 6=cfgUtcTime, field 33=cms_max_chg_soc, field 30=cfg_bms_power_off
    for fn, val in p.items():
        names = {1:'action_id', 2:'config_ok', 3:'cfg_power_off', 4:'cfg_power_on', 6:'cfgUtcTime',
                 30:'cfg_bms_power_off', 33:'cms_max_chg_soc', 34:'cms_min_dsg_soc'}
        name = names.get(fn, f'field_{fn}')
        if isinstance(val, bytes) and len(val) == 4:
            result[name] = struct.unpack('<f', val)[0]
        else:
            result[name] = val
    return result

def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload
    state['msg_count'] += 1

    # Parse outer setMessage
    outer = parse_fields(payload)
    inner_bytes = outer.get(1, b'')
    if not inner_bytes:
        return
    inner = parse_fields(inner_bytes)
    cf = inner.get(8, '?')
    ci = inner.get(9, '?')
    src = inner.get(2, '?')

    # Check for replies
    if 'set_reply' in topic:
        pdata_bytes = b''
        if isinstance(inner.get(1), bytes):
            pdata_bytes = inner.get(1, b'')
        decoded = decode_ack(pdata_bytes)
        print(f"\n{'='*60}")
        print(f"*** ACK RECEIVED on {topic} ***")
        print(f"  cf={cf}, ci={ci}, src={src}")
        print(f"  ACK values: {decoded}")
        ack_values.append(decoded)
        print(f"{'='*60}")
        return

    # Telemetry
    pdata_bytes = inner.get(1, b'')
    if isinstance(pdata_bytes, bytes) and len(pdata_bytes) > 0:
        pdata = parse_fields(pdata_bytes)
        if 518 in pdata and isinstance(pdata[518], bytes) and len(pdata[518]) == 4:
            state['batt_w'] = struct.unpack('<f', pdata[518])[0]
        if 515 in pdata and isinstance(pdata[515], bytes) and len(pdata[515]) == 4:
            pass  # grid
        if 1009 in pdata and isinstance(pdata[1009], bytes):
            sub = parse_fields(pdata[1009])
            if 5 in sub and isinstance(sub[5], bytes) and len(sub[5]) == 4:
                state['soc'] = struct.unpack('<f', sub[5])[0]

    dev  = 'ESG' if SN_ESG in topic else 'DPU'
    batt = f"batt={state['batt_w']:.0f}W" if state['batt_w'] else "batt=None"
    soc  = f"soc={state['soc']:.0f}%" if state['soc'] else ""
    print(f"  [{state['msg_count']:3d}] {dev} ci={ci} {batt} {soc}")

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected")
        topics = [
            (DATA_TOPIC_ESG, 0), (DATA_TOPIC_DPU, 0),
            (f'/app/{USER_ID}/{SN_DPU}/thing/property/set_reply', 0),
            (f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply', 0),
        ]
        client.subscribe(topics)
        print("Subscribed")
    else:
        print(f"Connect failed rc={rc}")

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
client.username_pw_set(MQTT_USER, MQTT_PASS)
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE
client.tls_set_context(ssl_ctx)
client.on_connect = on_connect
client.on_message = on_message
client.connect('mqtt.ecoflow.com', 8883, 60)
client.loop_start()
time.sleep(3)

# Trigger telemetry
for sn in [SN_ESG, SN_DPU]:
    client.publish(f'/app/{USER_ID}/{sn}/thing/property/get', json.dumps({
        "from": "HomeAssistant", "id": "999901234",
        "version": "1.1", "moduleType": 0, "operateType": "latestQuotas", "params": {}
    }), qos=1)

print("Waiting 8s for baseline telemetry...")
time.sleep(8)
print(f"\nBaseline: batt_w={state['batt_w']}, soc={state['soc']}")
print(f"  -> Battery is {'CHARGING' if state['batt_w'] and state['batt_w'] > 100 else 'IDLE/DISCHARGING'}")

ts = int(time.time())

print("\n" + "="*60)
print("STEP 1: Send cfg_bms_power_off=1 (stop BMS/charging)")
print("="*60)
pdata = pb_varint(6, ts) + pb_varint(30, 1)   # cfgUtcTime + cfg_bms_power_off=1
cmd = build_dpu_cmd(pdata)
print(f"Hex: {cmd.hex()}")
rc = client.publish(SET_TOPIC_DPU, cmd, qos=0)
print(f"Published rc={rc.rc}")

print("Waiting 8s for reply and effect...")
time.sleep(8)
print(f"  batt_w now: {state['batt_w']}")

print("\n" + "="*60)
print("STEP 2: Send cms_max_chg_soc=0 (set max charge SOC to 0%)")
print("="*60)
ts = int(time.time())
pdata = pb_varint(6, ts) + pb_varint(33, 0)   # cfgUtcTime + cms_max_chg_soc=0
cmd = build_dpu_cmd(pdata)
rc = client.publish(SET_TOPIC_DPU, cmd, qos=0)
print(f"Published rc={rc.rc}")

print("Waiting 8s for reply and effect...")
time.sleep(8)
print(f"  batt_w now: {state['batt_w']}")

print("\n" + "="*60)
print("STEP 3: Send cfg_power_off=1 (power off the unit)")
print("="*60)
ts = int(time.time())
pdata = pb_varint(6, ts) + pb_varint(3, 1)   # cfgUtcTime + cfg_power_off=1
cmd = build_dpu_cmd(pdata)
rc = client.publish(SET_TOPIC_DPU, cmd, qos=0)
print(f"Published rc={rc.rc}")

print("Waiting 8s for reply and effect...")
time.sleep(8)
print(f"  batt_w now: {state['batt_w']}")

print("\n" + "="*60)
print("STEP 4: RESTORE - cfg_bms_power_off=0 + cms_max_chg_soc=100")
print("="*60)
ts = int(time.time())
# First restore max charge SOC to 100
pdata = pb_varint(6, ts) + pb_varint(33, 100) + pb_varint(34, 0)  # maxSoc=100, minDsg=0
cmd = build_dpu_cmd(pdata)
rc = client.publish(SET_TOPIC_DPU, cmd, qos=0)
print(f"Restore maxSoc=100 published rc={rc.rc}")
time.sleep(3)

# Restore BMS power on
ts = int(time.time())
pdata = pb_varint(6, ts) + pb_varint(30, 0)  # cfg_bms_power_off=0 (back on)
cmd = build_dpu_cmd(pdata)
rc = client.publish(SET_TOPIC_DPU, cmd, qos=0)
print(f"Restore BMS on published rc={rc.rc}")

time.sleep(5)
print(f"\nFinal batt_w: {state['batt_w']}, soc={state['soc']}")

print("\n" + "="*60)
print("ALL ACKs received:")
for i, ack in enumerate(ack_values):
    print(f"  [{i+1}] {ack}")
print(f"Total MQTT messages: {state['msg_count']}")
print("="*60)

client.loop_stop()
client.disconnect()
