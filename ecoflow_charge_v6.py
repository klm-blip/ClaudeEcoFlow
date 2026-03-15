#!/usr/bin/env python3
"""
EcoFlow charge control v6 - Target ESG (HR65, SHP3) with ProtoPushAndSet.

Research:
- ESG (HR65) is a SHP3 = Smart Home Panel 3, closely related to SHP2 (HD31)
- SHP2 ioBroker data: cmdFunc=12, cmdId=32 = ProtoPushAndSet (device->app state report)
- SHP2 BLE (ef-ble-reverse): app sends with src=33, dst=11, cmd_func=12, cmd_id=33
- ESG telemetry: src=11, dest=32, version=4, cmdFunc=254, cmdId=21
- ProtoPushAndSet fields:
    foce_charge_hight   = field 6 (max charge SOC %)
    charge_watt_power   = field 7 (AC charge watts)
    localTime           = field 64 (unix timestamp - equiv of cfgUtcTime on DPUX)
    smart_backup_mode   = field 61 (0=off, 2=?)

Strategy: Try ESG with cmdFunc=12, cmdId=32 and cmdId=33, various dest/version combos.
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
SET_TOPIC_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/set'
DATA_TOPIC_ESG = f'/app/device/property/{SN_ESG}'
DATA_TOPIC_DPU = f'/app/device/property/{SN_DPU}'
SET_REPLY_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply'
SET_REPLY_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/set_reply'
GET_REPLY_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/get_reply'

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

def build_esg_cmd(pdata_bytes, cmd_id=32, dest=11, version=4, from_str='Android'):
    """Build a command for ESG (SHP3):
    cmdFunc=12 (ProtoPushAndSet family), configurable cmdId/dest/version.
    pdata is ProtoPushAndSet bytes.
    """
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    header = (
        pb_bytes(1,  pdata_bytes) +
        pb_varint(2, 32) +           # src=32 (app)
        pb_varint(3, dest) +         # dest
        pb_varint(4, 1) +            # d_src=1
        pb_varint(5, 1) +            # d_dest=1
        pb_varint(8, 12) +           # cmd_func=12
        pb_varint(9, cmd_id) +       # cmd_id
        pb_varint(10, len(pdata_bytes)) +
        pb_varint(11, 1) +           # need_ack=1
        pb_varint(14, seq) +
        pb_varint(16, version) +     # version
        pb_varint(17, 1) +           # payload_ver=1
        pb_string(23, from_str)
    )
    return pb_bytes(1, header)

# Also try without from_str for some variants
def build_esg_cmd_bare(pdata_bytes, cmd_id=32, dest=11, version=4):
    """Build ESG command without from string."""
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    header = (
        pb_bytes(1,  pdata_bytes) +
        pb_varint(2, 32) +
        pb_varint(3, dest) +
        pb_varint(4, 1) +
        pb_varint(5, 1) +
        pb_varint(8, 12) +
        pb_varint(9, cmd_id) +
        pb_varint(10, len(pdata_bytes)) +
        pb_varint(11, 1) +
        pb_varint(14, seq) +
        pb_varint(16, version) +
        pb_varint(17, 1)
    )
    return pb_bytes(1, header)

# ── State ─────────────────────────────────────────────────────────
state = {
    'batt_w': None,
    'soc': None,
    'msg_count': 0,
    'esg_ci_counts': {},
    'replies': [],
}

def decode_reply(pdata_bytes, label=''):
    """Decode a reply pdata and print all fields."""
    p = parse_fields(pdata_bytes)
    # Known ProtoPushAndSet fields
    esg_field_names = {
        1: 'grid_vol', 2: 'grid_freq', 3: 'product_type', 5: 'eps_mode_info',
        6: 'foce_charge_hight', 7: 'charge_watt_power', 8: 'disc_lower',
        9: 'power_sta', 10: 'master_cur', 14: 'is_get_cfg_flag',
        15: 'has_config_done', 16: 'is_area_err',
        18: 'ch1_force_charge', 19: 'ch2_force_charge', 20: 'ch3_force_charge',
        21: 'storm_is_enable', 24: 'ch1_enable_set', 25: 'ch2_enable_set',
        26: 'ch3_enable_set', 61: 'smart_backup_mode',
        62: 'backup_reserve_enable', 63: 'backup_reserve_soc',
        64: 'local_time', 66: 'time_zone',
        # ConfigWriteAck fields (if DPUX-style)
        1: 'action_id', 2: 'config_ok',
    }
    result = {}
    for fn, val in p.items():
        name = esg_field_names.get(fn, f'field_{fn}')
        if isinstance(val, bytes) and len(val) == 4:
            try:
                result[name] = struct.unpack('<f', val)[0]
            except:
                result[name] = val.hex()
        elif isinstance(val, bytes):
            result[name] = f'bytes[{len(val)}]={val[:8].hex()}'
        else:
            result[name] = val
    if label:
        print(f"  Reply decoded ({label}): {result}")
    return result

def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload
    state['msg_count'] += 1

    # Parse outer
    outer = parse_fields(payload)
    inner_bytes = outer.get(1, b'')
    if not inner_bytes:
        return
    inner = parse_fields(inner_bytes)
    cf = inner.get(8, '?')
    ci = inner.get(9, '?')
    src = inner.get(2, '?')
    dest = inner.get(3, '?')
    ver = inner.get(16, '?')

    # Check for SET replies (very important!)
    if 'set_reply' in topic or 'get_reply' in topic:
        pdata_bytes = inner.get(1, b'') if isinstance(inner.get(1), bytes) else b''
        print(f"\n{'='*70}")
        print(f"*** REPLY on {topic} ***")
        print(f"  cf={cf}, ci={ci}, src={src}, dest={dest}, ver={ver}")
        print(f"  pdata size: {len(pdata_bytes)} bytes")
        if pdata_bytes:
            decode_reply(pdata_bytes, f'cf={cf} ci={ci}')
        state['replies'].append({'topic': topic, 'cf': cf, 'ci': ci, 'src': src})
        print(f"{'='*70}")
        return

    # Track ESG ci values
    if SN_ESG in topic:
        key = f'cf{cf}_ci{ci}'
        state['esg_ci_counts'][key] = state['esg_ci_counts'].get(key, 0) + 1

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
    print(f"  [{state['msg_count']:3d}] {dev} cf={cf} ci={ci} {batt} {soc}")

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected")
        topics = [
            (DATA_TOPIC_ESG, 0), (DATA_TOPIC_DPU, 0),
            (SET_REPLY_ESG, 0), (SET_REPLY_DPU, 0),
            (GET_REPLY_ESG, 0),
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

print("Waiting 10s for baseline telemetry...")
time.sleep(10)
batt_baseline = state['batt_w']
soc_baseline  = state['soc']
print(f"\nBaseline: batt_w={batt_baseline}, soc={soc_baseline}")
print(f"ESG message types seen: {state['esg_ci_counts']}")
charging_status = 'CHARGING' if batt_baseline and batt_baseline > 200 else 'IDLE/DISCHARGING'
print(f"  -> Battery is {charging_status}")

# ─────────────────────────────────────────────────────────────────
# TESTS: ESG SET commands using ProtoPushAndSet (cmdFunc=12)
# ─────────────────────────────────────────────────────────────────
tests = [
    # Label, cmd_id, dest, version, from_str, pdata_builder
    # A: cmdId=32 (what device sends: ProtoPushAndSet), dest=11, version=4 - NO localTime
    ('A1-ci32-d11-v4-noTS-maxSoc0',        32, 11, 4, 'Android', lambda: pb_varint(6, 0)),
    # A: cmdId=32 - WITH localTime (field 64)
    ('A2-ci32-d11-v4-localTime-maxSoc0',   32, 11, 4, 'Android', lambda: pb_varint(64, int(time.time())) + pb_varint(6, 0)),
    # A: cmdId=32 - version=3 (DPUX version) - WITH localTime
    ('A3-ci32-d11-v3-localTime-maxSoc0',   32, 11, 3, 'Android', lambda: pb_varint(64, int(time.time())) + pb_varint(6, 0)),
    # A: cmdId=32 - dest=32 like latestQuotas
    ('A4-ci32-d32-v4-localTime-maxSoc0',   32, 32, 4, 'Android', lambda: pb_varint(64, int(time.time())) + pb_varint(6, 0)),
    # B: cmdId=33 (what BLE app sends: shp2cmd/ProtoPushAndSet), dest=11, version=4
    ('B1-ci33-d11-v4-noTS-maxSoc0',        33, 11, 4, 'Android', lambda: pb_varint(6, 0)),
    # B: cmdId=33 - WITH localTime
    ('B2-ci33-d11-v4-localTime-maxSoc0',   33, 11, 4, 'Android', lambda: pb_varint(64, int(time.time())) + pb_varint(6, 0)),
    # B: cmdId=33 - version=3
    ('B3-ci33-d11-v3-localTime-maxSoc0',   33, 11, 3, 'Android', lambda: pb_varint(64, int(time.time())) + pb_varint(6, 0)),
    # C: chargeWattPower=500 (min watt) instead of maxSoc=0
    ('C1-ci32-d11-v4-localTime-500W',      32, 11, 4, 'Android', lambda: pb_varint(64, int(time.time())) + pb_varint(7, 500)),
    ('C2-ci33-d11-v4-localTime-500W',      33, 11, 4, 'Android', lambda: pb_varint(64, int(time.time())) + pb_varint(7, 500)),
    # D: ios from_str (ioBroker uses 'iOS')
    ('D1-ci32-d11-v4-ios-maxSoc0',         32, 11, 4, 'ios', lambda: pb_varint(64, int(time.time())) + pb_varint(6, 0)),
    ('D2-ci33-d11-v4-ios-maxSoc0',         33, 11, 4, 'ios', lambda: pb_varint(64, int(time.time())) + pb_varint(6, 0)),
    # E: dest=11 but no from_str
    ('E1-ci32-d11-v4-noFrom-maxSoc0',      32, 11, 4, '', lambda: pb_varint(64, int(time.time())) + pb_varint(6, 0)),
]

print(f"\n{'='*70}")
print(f"TESTING {len(tests)} ESG COMMAND VARIANTS")
print(f"SET topic: {SET_TOPIC_ESG}")
print(f"{'='*70}")

for label, cmd_id, dest, version, from_str, pdata_fn in tests:
    print(f"\n>>> {label}")
    pdata = pdata_fn()
    if from_str:
        cmd = build_esg_cmd(pdata, cmd_id=cmd_id, dest=dest, version=version, from_str=from_str)
    else:
        cmd = build_esg_cmd_bare(pdata, cmd_id=cmd_id, dest=dest, version=version)
    print(f"    hex({len(cmd)}B): {cmd.hex()[:80]}...")
    rc = client.publish(SET_TOPIC_ESG, cmd, qos=0)
    print(f"    Published rc={rc.rc}")
    time.sleep(8)
    batt_now = state['batt_w']
    delta = (batt_now - batt_baseline) if (batt_now and batt_baseline) else None
    print(f"    batt_w now: {batt_now} (delta: {delta:+.0f}W)" if delta is not None else f"    batt_w now: {batt_now}")
    replies_so_far = len(state['replies'])
    if replies_so_far > 0:
        print(f"    *** {replies_so_far} REPLIES RECEIVED SO FAR! ***")
        print(f"    Breaking test loop - we have a reply!")
        break

# ─────────────────────────────────────────────────────────────────
# Final state
# ─────────────────────────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"FINAL STATE: batt_w={state['batt_w']}, soc={state['soc']}")
print(f"ESG message types: {state['esg_ci_counts']}")
print(f"Total replies: {len(state['replies'])}")
for i, r in enumerate(state['replies']):
    print(f"  [{i+1}] {r}")
print(f"Total MQTT messages: {state['msg_count']}")

# ─────────────────────────────────────────────────────────────────
# If we stopped charging, restore it
# ─────────────────────────────────────────────────────────────────
if state['batt_w'] and batt_baseline and state['batt_w'] < batt_baseline - 500:
    print(f"\n*** BATTERY CHARGE CHANGED - RESTORING maxSoc=100 and chargeWattPower=3000 ***")
    # Restore: foce_charge_hight=100, charge_watt_power=3000
    ts = int(time.time())
    restore_pdata = pb_varint(64, ts) + pb_varint(6, 100) + pb_varint(7, 3000)
    restore_cmd = build_esg_cmd(restore_pdata, cmd_id=32, dest=11, version=4, from_str='Android')
    client.publish(SET_TOPIC_ESG, restore_cmd, qos=0)
    print("  Restore command sent")
    time.sleep(5)
    print(f"  Final batt_w after restore: {state['batt_w']}")

print(f"{'='*70}")

client.loop_stop()
client.disconnect()
