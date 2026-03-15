#!/usr/bin/env python3
"""
EcoFlow charge control v7 - ESG ConfigWrite CONFIRMED WORKING!

BREAKTHROUGH from hexdump test:
- ESG (HR65) accepts ConfigWrite (cmdFunc=254, cmdId=17) with version=3, dest=11
- ESG responds with ConfigWriteAck (cmdFunc=254, cmdId=18) config_ok=1
- cfgUtcTime (field 6) confirmed accepted
- version=3 required (version=4 didn't trigger a response)

Now test charging control fields using DPUX schema on ESG:
- field 33 = cms_max_chg_soc (max charge SOC %)
- field 30 = cfg_bms_power_off (BMS power off)
- field 3  = cfg_power_off
- Also try SHP2 schema fields:
  - field 7 = charge_watt_power (AC charge watts)
  - field 6+value = foce_charge_hight (max SOC % in SHP2 schema - but conflicts with cfgUtcTime!)
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

SET_TOPIC_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set'
DATA_TOPIC_ESG = f'/app/device/property/{SN_ESG}'
DATA_TOPIC_DPU = f'/app/device/property/{SN_DPU}'
SET_REPLY_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply'
SET_REPLY_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/set_reply'

# ── Protobuf helpers ─────────────────────────────────────────────
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
        if wire_type == 0:
            val = 0; shift = 0
            while i < len(payload):
                b = payload[i]; i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            fields[field_num] = val
        elif wire_type == 2:
            length = 0; shift = 0
            while i < len(payload):
                b = payload[i]; i += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            fields[field_num] = payload[i:i+length]
            i += length
        elif wire_type == 5:
            fields[field_num] = payload[i:i+4]
            i += 4
        else: break
    return fields

def build_esg_cmd(pdata_bytes):
    """Build ConfigWrite for ESG: dest=11, cmdFunc=254, cmdId=17, version=3."""
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    header = (
        pb_bytes(1,  pdata_bytes) +
        pb_varint(2, 32) +   # src=32 (app)
        pb_varint(3, 11) +   # dest=11 (ESG)
        pb_varint(4, 1) +    # d_src=1
        pb_varint(5, 1) +    # d_dest=1
        pb_varint(8, 254) +  # cmd_func=254
        pb_varint(9, 17) +   # cmd_id=17 (ConfigWrite)
        pb_varint(10, len(pdata_bytes)) +
        pb_varint(11, 1) +   # need_ack=1
        pb_varint(14, seq) +
        pb_varint(16, 3) +   # version=3 (CONFIRMED WORKING!)
        pb_varint(17, 1) +   # payload_ver=1
        pb_string(23, 'Android')
    )
    return pb_bytes(1, header)

def decode_ack(pdata_bytes):
    """Decode ConfigWriteAck."""
    p = parse_fields(pdata_bytes)
    field_names = {
        1: 'action_id', 2: 'config_ok', 3: 'cfg_power_off', 4: 'cfg_power_on',
        6: 'cfgUtcTime', 7: 'charge_watt_power',
        30: 'cfg_bms_power_off', 33: 'cms_max_chg_soc', 34: 'cms_min_dsg_soc'
    }
    result = {}
    for fn, val in p.items():
        name = field_names.get(fn, f'field_{fn}')
        if isinstance(val, bytes) and len(val) == 4:
            result[name] = struct.unpack('<f', val)[0]
        else:
            result[name] = val
    return result

# ── State ─────────────────────────────────────────────────────────
state = {'batt_w': None, 'soc': None, 'msg_count': 0, 'replies': []}

def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload
    state['msg_count'] += 1

    outer = parse_fields(payload)
    inner_bytes = outer.get(1, b'')
    if not inner_bytes:
        return
    inner = parse_fields(inner_bytes)
    cf = inner.get(8, '?')
    ci = inner.get(9, '?')
    src = inner.get(2, '?')
    ver = inner.get(16, '?')

    if 'set_reply' in topic:
        pdata_bytes = inner.get(1, b'') if isinstance(inner.get(1), bytes) else b''
        ack = decode_ack(pdata_bytes)
        print(f"\n{'='*70}")
        print(f"*** ESG ACK on {topic} ***")
        print(f"  cf={cf}, ci={ci}, src={src}, ver={ver}")
        print(f"  ACK: {ack}")
        state['replies'].append(ack)
        print(f"{'='*70}")
        return

    # Telemetry
    pdata_bytes = inner.get(1, b'')
    if isinstance(pdata_bytes, bytes) and len(pdata_bytes) > 0:
        pdata = parse_fields(pdata_bytes)
        if 518 in pdata and isinstance(pdata[518], bytes) and len(pdata[518]) == 4:
            state['batt_w'] = struct.unpack('<f', pdata[518])[0]
        if 1009 in pdata and isinstance(pdata[1009], bytes):
            sub = parse_fields(pdata[1009])
            if 5 in sub and isinstance(sub[5], bytes) and len(sub[5]) == 4:
                state['soc'] = struct.unpack('<f', sub[5])[0]

    dev  = 'ESG' if SN_ESG in topic else 'DPU'
    batt = f"batt={state['batt_w']:.0f}W" if state['batt_w'] else "batt=?"
    soc  = f"soc={state['soc']:.0f}%" if state['soc'] else ""
    print(f"  [{state['msg_count']:3d}] {dev} {batt} {soc}")

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected")
        client.subscribe([
            (DATA_TOPIC_ESG, 0), (DATA_TOPIC_DPU, 0),
            (SET_REPLY_ESG, 0), (SET_REPLY_DPU, 0)
        ])
        print("Subscribed")
    else:
        print(f"Connect failed rc={rc}")

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
client.username_pw_set(MQTT_USER, MQTT_PASS)
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False; ssl_ctx.verify_mode = ssl.CERT_NONE
client.tls_set_context(ssl_ctx)
client.on_connect = on_connect; client.on_message = on_message
client.connect('mqtt.ecoflow.com', 8883, 60)
client.loop_start()
time.sleep(3)

# Trigger telemetry
for sn in [SN_ESG, SN_DPU]:
    client.publish(f'/app/{USER_ID}/{sn}/thing/property/get', json.dumps({
        "from": "HomeAssistant", "id": "999901234",
        "version": "1.1", "moduleType": 0, "operateType": "latestQuotas", "params": {}
    }), qos=1)

print("Waiting 10s for baseline telemetry...")
time.sleep(10)
batt_baseline = state['batt_w']
soc_baseline  = state['soc']
print(f"\nBaseline: batt_w={batt_baseline}, soc={soc_baseline}")
charging = 'CHARGING' if batt_baseline and batt_baseline > 200 else 'IDLE/DISCHARGING'
print(f"  -> Battery is {charging}")

print(f"\n{'='*70}")
print("ESG ConfigWrite tests (cf=254/ci=17, dest=11, version=3)")
print("CONFIRMED FORMAT from hexdump test")
print(f"{'='*70}")

def run_esg_test(label, pdata):
    print(f"\n>>> {label}")
    cmd = build_esg_cmd(pdata)
    print(f"  pdata hex: {pdata.hex()}")
    rc = client.publish(SET_TOPIC_ESG, cmd, qos=0)
    print(f"  Published rc={rc.rc}")
    time.sleep(8)
    batt_now = state['batt_w']
    if batt_now and batt_baseline:
        delta = batt_now - batt_baseline
        print(f"  batt_w: {batt_now:.0f}W (delta: {delta:+.0f}W vs baseline {batt_baseline:.0f}W)")
    else:
        print(f"  batt_w: {batt_now}")
    print(f"  ACKs so far: {len(state['replies'])}")

ts = int(time.time())

# STEP 1: Verify cfgUtcTime alone still works (re-confirm the format)
run_esg_test("STEP 1: cfgUtcTime only (re-verify format works)",
    pb_varint(6, ts))
ts = int(time.time())

# STEP 2: cms_max_chg_soc=0 (DPUX field 33 - set max charge SOC to 0%)
# This WORKED on DPUX (got config_ok=1 but didn't stop charging because ESG controls it)
# Now sending TO ESG - if ESG uses same schema, this may stop charging!
run_esg_test("STEP 2: cms_max_chg_soc=0 (field 33) - STOP CHARGING",
    pb_varint(6, ts) + pb_varint(33, 0))
ts = int(time.time())

# STEP 3: cfg_bms_power_off=1 (DPUX field 30)
run_esg_test("STEP 3: cfg_bms_power_off=1 (field 30)",
    pb_varint(6, ts) + pb_varint(30, 1))
ts = int(time.time())

# STEP 4: charge_watt_power=500 (SHP2 ProtoPushAndSet field 7)
# In case ESG uses SHP2 schema instead of DPUX schema for ConfigWrite
run_esg_test("STEP 4: charge_watt_power=500 (field 7, SHP2-style)",
    pb_varint(6, ts) + pb_varint(7, 500))
ts = int(time.time())

# STEP 5: cfg_power_off=1 (field 3) - try to power off the system
run_esg_test("STEP 5: cfg_power_off=1 (field 3)",
    pb_varint(6, ts) + pb_varint(3, 1))
ts = int(time.time())

# ── Summary ──────────────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"RESULTS:")
print(f"  Baseline batt_w: {batt_baseline}")
print(f"  Final batt_w: {state['batt_w']}")
print(f"  soc: {state['soc']}")
print(f"\n  ACKs received ({len(state['replies'])} total):")
for i, ack in enumerate(state['replies']):
    print(f"    [{i+1}] {ack}")
print(f"  Total MQTT messages: {state['msg_count']}")
print(f"{'='*70}")

# If charging changed, restore
if state['batt_w'] and batt_baseline:
    delta = state['batt_w'] - batt_baseline
    if delta < -500:
        print(f"\n*** BATTERY CHARGE STOPPED (delta={delta:+.0f}W) - RESTORING ***")
        ts = int(time.time())
        restore = pb_varint(6, ts) + pb_varint(33, 100) + pb_varint(34, 0) + pb_varint(30, 0)
        cmd = build_esg_cmd(restore)
        client.publish(SET_TOPIC_ESG, cmd, qos=0)
        print("  Restore command sent")
        time.sleep(5)
        print(f"  Final batt_w after restore: {state['batt_w']}")
    elif delta > 0:
        print(f"\nBattery still charging (delta={delta:+.0f}W)")

client.loop_stop()
client.disconnect()
