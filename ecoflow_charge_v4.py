#!/usr/bin/env python3
"""
EcoFlow charge control v4 - comprehensive approach.
Tests JSON cmdCode formats + protobuf with cfgUtcTime + JT-S1 format.
Battery MUST be charging to observe effects.
"""
import ssl, json, time, struct
import paho.mqtt.client as mqtt

# ── credentials ────────────────────────────────────────────────
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
GET_TOPIC_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/get'
GET_TOPIC_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/get'
DATA_TOPIC_ESG = f'/app/device/property/{SN_ESG}'
DATA_TOPIC_DPU = f'/app/device/property/{SN_DPU}'

# ── Protobuf helpers (manual encoding) ─────────────────────────
def pb_varint(field, value):
    """Encode a varint protobuf field."""
    tag = (field << 3) | 0
    result = b''
    v = tag
    while v > 0x7F:
        result += bytes([0x80 | (v & 0x7F)])
        v >>= 7
    result += bytes([v & 0x7F])
    v = value
    while v > 0x7F:
        result += bytes([0x80 | (v & 0x7F)])
        v >>= 7
    result += bytes([v & 0x7F])
    return result

def pb_string(field, value):
    """Encode a string protobuf field."""
    tag = (field << 3) | 2
    result = b''
    v = tag
    while v > 0x7F:
        result += bytes([0x80 | (v & 0x7F)])
        v >>= 7
    result += bytes([v & 0x7F])
    enc = value.encode('utf-8')
    length = len(enc)
    while length > 0x7F:
        result += bytes([0x80 | (length & 0x7F)])
        length >>= 7
    result += bytes([length & 0x7F])
    result += enc
    return result

def pb_bytes(field, value):
    """Encode a bytes/embedded-message protobuf field."""
    tag = (field << 3) | 2
    result = b''
    v = tag
    while v > 0x7F:
        result += bytes([0x80 | (v & 0x7F)])
        v >>= 7
    result += bytes([v & 0x7F])
    length = len(value)
    while length > 0x7F:
        result += bytes([0x80 | (length & 0x7F)])
        length >>= 7
    result += bytes([length & 0x7F])
    result += value
    return result

def pb_float(field, value):
    """Encode a float32 protobuf field."""
    tag = (field << 3) | 5
    result = b''
    v = tag
    while v > 0x7F:
        result += bytes([0x80 | (v & 0x7F)])
        v >>= 7
    result += bytes([v & 0x7F])
    result += struct.pack('<f', value)
    return result

def build_proto_cmd(pdata_bytes, src=32, dest=2, d_src=1, d_dest=1,
                    cmd_func=254, cmd_id=17, version=3, product_id=0,
                    need_ack=1, from_str='Android', device_sn=''):
    """Build a setMessage protobuf command."""
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    header = (
        pb_bytes(1,  pdata_bytes) +    # pdata
        pb_varint(2, src) +             # src
        pb_varint(3, dest) +            # dest
        pb_varint(4, d_src) +           # d_src
        pb_varint(5, d_dest) +          # d_dest
        pb_varint(8, cmd_func) +        # cmd_func
        pb_varint(9, cmd_id) +          # cmd_id
        pb_varint(10, len(pdata_bytes)) + # data_len
        pb_varint(11, need_ack) +       # need_ack
        pb_varint(14, seq) +            # seq
        pb_varint(16, version) +        # version
        pb_varint(17, 1)                # payload_ver
    )
    if product_id > 0:
        header += pb_varint(15, product_id)
    if from_str:
        header += pb_string(23, from_str)
    if device_sn:
        header += pb_string(25, device_sn)
    return pb_bytes(1, header)

# ── JSON command builder ────────────────────────────────────────
def make_json_cmd(data_dict):
    """Build a JSON command payload."""
    payload = {
        "from": "HomeAssistant",
        "id": str(int(time.time() * 1000)),
        "version": "1.0",
    }
    payload.update(data_dict)
    return json.dumps(payload).encode()

def make_json_cmd_v11(operate_type, params):
    """Build a JSON command with v1.1 format (like latestQuotas)."""
    return json.dumps({
        "from": "HomeAssistant",
        "id": str(int(time.time() * 1000)),
        "version": "1.1",
        "moduleType": 0,
        "operateType": operate_type,
        "params": params
    }).encode()

# ── state tracking ─────────────────────────────────────────────
state = {
    'batt_w': None,
    'grid_w': None,
    'soc':    None,
    'msg_count': 0,
    'replies': [],
}

def parse_fields(payload):
    """Very simple protobuf field parser for monitoring."""
    fields = {}
    i = 0
    while i < len(payload):
        if payload[i] == 0:
            break
        tag = 0
        shift = 0
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
        else:
            break
    return fields

def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload
    state['msg_count'] += 1

    # Try to detect JSON vs protobuf
    if payload and payload[0:1] in (b'{', b'['):
        print(f"[{state['msg_count']:3d}] JSON on {topic}: {payload[:200].decode(errors='replace')}")
        state['replies'].append(('JSON', topic, payload.decode(errors='replace')))
        return

    # Protobuf decode
    try:
        outer = parse_fields(payload)
        inner_bytes = outer.get(1, b'')
        if not inner_bytes:
            return
        inner = parse_fields(inner_bytes)
        cf = inner.get(8, '?')
        ci = inner.get(9, '?')
        src = inner.get(2, '?')
        dest = inner.get(3, '?')
        v = inner.get(16, '?')

        # Check for set/reply topics
        if 'set_reply' in topic or 'get_reply' in topic:
            print(f"\n*** REPLY on {topic} cf={cf} ci={ci} src={src} dest={dest} v={v} ***")
            state['replies'].append(('REPLY', topic, {'cf': cf, 'ci': ci}))
            # Decode pdata too
            pdata = inner_bytes
            if isinstance(inner.get(1), bytes):
                pdata = inner.get(1, b'')
            p = parse_fields(pdata)
            if p:
                print(f"    pdata fields: {list(p.keys())}")
            return

        # Decode pdata for battery/grid/SOC
        pdata_bytes = inner.get(1, b'')
        if isinstance(pdata_bytes, bytes) and len(pdata_bytes) > 0:
            pdata = parse_fields(pdata_bytes)
            # field 518 = battery watts (float32)
            if 518 in pdata and isinstance(pdata[518], bytes) and len(pdata[518]) == 4:
                bw = struct.unpack('<f', pdata[518])[0]
                state['batt_w'] = bw
            # field 515 = grid watts (float32)
            if 515 in pdata and isinstance(pdata[515], bytes) and len(pdata[515]) == 4:
                gw = struct.unpack('<f', pdata[515])[0]
                state['grid_w'] = gw
            # field 1009 SOC
            if 1009 in pdata:
                sub = parse_fields(pdata[1009]) if isinstance(pdata[1009], bytes) else {}
                if 5 in sub and isinstance(sub[5], bytes) and len(sub[5]) == 4:
                    state['soc'] = struct.unpack('<f', sub[5])[0]

        batt = f"batt={state['batt_w']:.0f}W" if state['batt_w'] is not None else "batt=None"
        soc  = f"soc={state['soc']:.0f}%" if state['soc'] is not None else ""
        dev  = 'ESG' if SN_ESG in topic else 'DPU'
        short_topic = 'set' if '/set' in topic else ('data' if 'property/HR' in topic or 'property/P1' in topic else topic.split('/')[-1])
        print(f"[{state['msg_count']:3d}] {dev}/{short_topic} cf={cf} ci={ci} {batt} {soc}")

    except Exception as e:
        pass

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected OK")
        # Subscribe to all relevant topics
        topics = [
            (DATA_TOPIC_ESG, 0),
            (DATA_TOPIC_DPU, 0),
            (f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply', 0),
            (f'/app/{USER_ID}/{SN_DPU}/thing/property/set_reply', 0),
            (f'/app/{USER_ID}/{SN_ESG}/thing/property/get_reply', 0),
            (f'/app/{USER_ID}/{SN_DPU}/thing/property/get_reply', 0),
        ]
        client.subscribe(topics)
        print("Subscribed to all topics")
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
    get_topic = f'/app/{USER_ID}/{sn}/thing/property/get'
    client.publish(get_topic, json.dumps({
        "from": "HomeAssistant",
        "id": "999901234",
        "version": "1.1",
        "moduleType": 0,
        "operateType": "latestQuotas",
        "params": {}
    }), qos=1)
time.sleep(8)

print(f"\nBaseline: batt={state['batt_w']}, grid={state['grid_w']}, soc={state['soc']}")
if state['batt_w'] is None:
    print("WARNING: Battery not charging. Commands may be harder to observe.")

print("\n" + "="*60)
print("Starting command tests. Will try 12 variants over ~60 seconds.")
print("="*60)

# ── Define all test commands ────────────────────────────────────
timestamp = int(time.time())

# GROUP 1: JSON commands on consumer MQTT (NEVER TRIED BEFORE)
json_tests = [
    # 1a. YJ751 cmdCode on DPU topic (delta pro ultra style)
    ("1a-JSON-YJ751-maxChgSoc-DPU", SET_TOPIC_DPU, make_json_cmd({
        "sn": SN_DPU,
        "cmdCode": "YJ751_PD_CHG_SOC_MAX_SET",
        "params": {"maxChgSoc": 20}
    })),
    # 1b. YJ751 AC chg pause on DPU topic
    ("1b-JSON-YJ751-acChgPause-DPU", SET_TOPIC_DPU, make_json_cmd({
        "sn": SN_DPU,
        "cmdCode": "YJ751_PD_AC_CHG_SET",
        "params": {"chgPauseFlag": 1}
    })),
    # 1c. PD303 cmdCode on ESG topic (SHP2 style)
    ("1c-JSON-PD303-backupSoc-ESG", SET_TOPIC_ESG, make_json_cmd({
        "sn": SN_ESG,
        "cmdCode": "PD303_APP_SET",
        "params": {"backupReserveSoc": 15}
    })),
    # 1d. operateType TCP on DPU (private API style)
    ("1d-JSON-TCP-cmsMaxChgSoc-DPU", SET_TOPIC_DPU, make_json_cmd_v11("TCP", {"cmsMaxChgSoc": 20})),
    # 1e. latestQuotas-style but as set (wrong but let's see)
    ("1e-JSON-set-operateType-ESG", SET_TOPIC_ESG, make_json_cmd_v11("acChgCfg", {"chargeWattPower": 3000})),
]

# GROUP 2: Protobuf WITH cfgUtcTime (previously missing!)
def build_stream_with_timestamp(target_sn, soc_val):
    """stream_ultra ConfigWrite with cfgUtcTime (field 6) included."""
    pdata = (
        pb_varint(6, timestamp) +      # cfgUtcTime - CRITICAL MISSING FIELD
        pb_varint(33, soc_val)         # cms_max_chg_soc
    )
    return build_proto_cmd(pdata, dest=2, cmd_func=254, cmd_id=17, version=3, from_str='Android')

def build_jts1_bat_chg_dsg(target_chg=20, target_dsg=10, target_backup=15):
    """JT-S1 EMS_CMD_SETS=96, EMS_CMD_ID_SYS_BAT_CHG_DSG_SET=112"""
    pdata = (
        pb_varint(1, target_chg) +    # sys_bat_chg_up_limit (max charge SOC)
        pb_varint(2, target_dsg) +    # sys_bat_dsg_down_limie (min discharge SOC)
        pb_varint(3, target_backup)   # sys_bat_backup_ratio
    )
    return build_proto_cmd(pdata, dest=2, cmd_func=96, cmd_id=112, version=3, from_str='Android')

def build_shp3_proto(pdata, cmd_id):
    """SHP3/SHP2-style: dest=11, cmd_func=12, version=19."""
    return build_proto_cmd(pdata, dest=11, cmd_func=12, cmd_id=cmd_id, version=19, from_str='ios')

proto_tests = [
    # 2a. stream ConfigWrite WITH cfgUtcTime - max charge SOC = 20 (DPU topic)
    ("2a-Proto-ConfigWrite+utcTime-maxSoc20-DPU", SET_TOPIC_DPU,
     build_stream_with_timestamp(SN_DPU, 20)),
    # 2b. same to ESG topic
    ("2b-Proto-ConfigWrite+utcTime-maxSoc20-ESG", SET_TOPIC_ESG,
     build_stream_with_timestamp(SN_ESG, 20)),
    # 2c. JT-S1 EMS bat chg/dsg set - DPU topic
    ("2c-Proto-JTS1-BatChgDsg-DPU", SET_TOPIC_DPU,
     build_jts1_bat_chg_dsg(20, 10, 15)),
    # 2d. JT-S1 - ESG topic
    ("2d-Proto-JTS1-BatChgDsg-ESG", SET_TOPIC_ESG,
     build_jts1_bat_chg_dsg(20, 10, 15)),
    # 2e. SHP2-style ProtoPushAndSet with dest=11 - ESG topic
    #     ch1_force_charge (field 18 of ProtoPushAndSet) = 0 (OFF)
    #     in setHeader wrapper
    ("2e-Proto-SHP2-ForceChargeOFF-ESG", SET_TOPIC_ESG,
     build_shp3_proto(pb_varint(18, 0), cmd_id=33)),
    # 2f. stream ConfigWrite cfg_bms_power_off=1 WITH cfgUtcTime - ESG dest=11
    ("2f-Proto-ConfigWrite+utcTime-bmsPowerOff-dest11-ESG", SET_TOPIC_ESG,
     build_proto_cmd(
         pb_varint(6, timestamp) + pb_varint(30, 1),  # cfgUtcTime + cfg_bms_power_off=1
         dest=11, cmd_func=254, cmd_id=17, version=4, from_str='Android'
     )),
    # 2g. stream ConfigWrite version=4 dest=11 WITH cfgUtcTime
    ("2g-Proto-ConfigWrite+utcTime-maxSoc20-v4-dest11-ESG", SET_TOPIC_ESG,
     build_proto_cmd(
         pb_varint(6, timestamp) + pb_varint(33, 20),  # cfgUtcTime + cms_max_chg_soc=20
         dest=11, cmd_func=254, cmd_id=17, version=4, from_str='Android'
     )),
]

all_tests = json_tests + proto_tests

for name, topic, payload in all_tests:
    time.sleep(4)
    batt = f"{state['batt_w']:.0f}W" if state['batt_w'] is not None else "None"
    print(f"\n[TEST {name}]")
    print(f"  Topic: {topic}")
    print(f"  batt_w before: {batt}")
    if isinstance(payload, bytes) and payload[0:1] == b'{':
        print(f"  JSON: {payload[:100].decode()}")
    else:
        print(f"  hex: {payload.hex()[:60]}...")
    rc = client.publish(topic, payload, qos=0)
    print(f"  publish rc={rc.rc}")

print("\n\nWaiting 10 more seconds for any delayed replies...")
time.sleep(10)

print(f"\n{'='*60}")
print(f"FINAL STATE: batt_w={state['batt_w']}, grid_w={state['grid_w']}, soc={state['soc']}")
print(f"Total messages received: {state['msg_count']}")
print(f"Replies received: {len(state['replies'])}")
for r in state['replies']:
    print(f"  {r}")

client.loop_stop()
client.disconnect()
print("\nDone.")
