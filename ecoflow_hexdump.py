#!/usr/bin/env python3
"""
EcoFlow hex dump monitor - capture and print raw ESG vs DPU packet bytes.
Also test cmdFunc=254/ci=17 (ConfigWrite) on ESG, since version=19 in SHP2 sim
suggests different protocol. Want to see exact packet structure differences.
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

SET_TOPIC_ESG = f'/app/{USER_ID}/{SN_ESG}/thing/property/set'
SET_TOPIC_DPU = f'/app/{USER_ID}/{SN_DPU}/thing/property/set'
DATA_TOPIC_ESG = f'/app/device/property/{SN_ESG}'
DATA_TOPIC_DPU = f'/app/device/property/{SN_DPU}'
SET_REPLY_ESG = f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply'
SET_REPLY_DPU = f'/app/{USER_ID}/{SN_DPU}/thing/property/set_reply'

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

def parse_varint(data, offset):
    value = 0; shift = 0
    while offset < len(data):
        b = data[offset]; offset += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80): break
        shift += 7
    return value, offset

def parse_all_fields(payload):
    """Parse protobuf fields, return list of (field_num, wire_type, value)."""
    result = []
    i = 0
    while i < len(payload):
        if payload[i] == 0 and i == len(payload)-1: break
        try:
            tag, i = parse_varint(payload, i)
            field_num = tag >> 3
            wire_type = tag & 7
            if field_num == 0: break
            if wire_type == 0:
                val, i = parse_varint(payload, i)
                result.append((field_num, 'varint', val))
            elif wire_type == 2:
                length, i = parse_varint(payload, i)
                val = payload[i:i+length]
                i += length
                result.append((field_num, 'bytes', val))
            elif wire_type == 5:
                val = payload[i:i+4]
                i += 4
                result.append((field_num, 'fixed32', val))
            elif wire_type == 1:
                val = payload[i:i+8]
                i += 8
                result.append((field_num, 'fixed64', val))
            else:
                break
        except:
            break
    return result

def print_packet_structure(payload, prefix='', depth=0, max_depth=3):
    """Recursively print protobuf structure."""
    fields = parse_all_fields(payload)
    for (fn, wt, val) in fields:
        indent = '  ' * depth
        if wt == 'varint':
            print(f"{prefix}{indent}[f{fn}] varint = {val}")
        elif wt == 'bytes':
            # Try to detect if it's a nested protobuf (starts with valid tag)
            is_proto = False
            if len(val) > 0 and depth < max_depth:
                try:
                    sub_fields = parse_all_fields(val)
                    if len(sub_fields) > 0 and all(f[0] < 200 for f in sub_fields):
                        is_proto = True
                except:
                    pass
            if is_proto and len(val) > 2:
                print(f"{prefix}{indent}[f{fn}] bytes[{len(val)}] (nested proto):")
                print_packet_structure(val, prefix, depth+1, max_depth)
            else:
                # Float check
                if len(val) == 4:
                    try:
                        f_val = struct.unpack('<f', val)[0]
                        print(f"{prefix}{indent}[f{fn}] bytes[4] = {val.hex()} (float32: {f_val:.4f})")
                    except:
                        print(f"{prefix}{indent}[f{fn}] bytes[{len(val)}] = {val[:20].hex()}")
                else:
                    # Try string
                    try:
                        s = val.decode('utf-8')
                        if s.isprintable() and len(s) < 50:
                            print(f"{prefix}{indent}[f{fn}] string = '{s}'")
                        else:
                            print(f"{prefix}{indent}[f{fn}] bytes[{len(val)}] = {val[:20].hex()}...")
                    except:
                        print(f"{prefix}{indent}[f{fn}] bytes[{len(val)}] = {val[:20].hex()}...")
        elif wt == 'fixed32':
            try:
                f_val = struct.unpack('<f', val)[0]
                print(f"{prefix}{indent}[f{fn}] fixed32 = {val.hex()} (float32: {f_val:.4f})")
            except:
                print(f"{prefix}{indent}[f{fn}] fixed32 = {val.hex()}")

# ── State ────────────────────────────────────────────────────────
state = {'msg_count': 0, 'esg_packets': [], 'dpu_packets': [], 'batt_w': None, 'soc': None, 'replies': []}
CAPTURE_LIMIT = 3  # Capture first 3 packets per device for analysis

def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload
    state['msg_count'] += 1

    # Check for SET replies
    if 'set_reply' in topic:
        print(f"\n{'='*70}")
        print(f"!!! SET REPLY on {topic} !!!")
        print(f"Raw hex ({len(payload)}B): {payload.hex()}")
        print("Structure:")
        print_packet_structure(payload, '  ')
        state['replies'].append({'topic': topic, 'hex': payload.hex()})
        print(f"{'='*70}")
        return

    dev = 'ESG' if SN_ESG in topic else 'DPU'

    # Capture first few packets for analysis
    if dev == 'ESG' and len(state['esg_packets']) < CAPTURE_LIMIT:
        state['esg_packets'].append(payload)
        print(f"\n{'-'*60}")
        print(f"ESG PACKET #{len(state['esg_packets'])} ({len(payload)} bytes)")
        print(f"Hex: {payload.hex()}")
        print("Structure:")
        print_packet_structure(payload, '  ')
        print(f"{'-'*60}")
    elif dev == 'DPU' and len(state['dpu_packets']) < CAPTURE_LIMIT:
        state['dpu_packets'].append(payload)
        print(f"\n{'-'*60}")
        print(f"DPU PACKET #{len(state['dpu_packets'])} ({len(payload)} bytes)")
        print(f"Hex: {payload.hex()}")
        print("Structure:")
        print_packet_structure(payload, '  ')
        print(f"{'-'*60}")

    # Extract telemetry
    try:
        fields = parse_all_fields(payload)
        for fn, wt, val in fields:
            if fn == 1 and wt == 'bytes':
                inner_fields = parse_all_fields(val)
                for ifn, iwt, ival in inner_fields:
                    if ifn == 1 and iwt == 'bytes':
                        pdata_fields = parse_all_fields(ival)
                        for pfn, pwt, pval in pdata_fields:
                            if pfn == 518 and pwt == 'bytes' and len(pval) == 4:
                                state['batt_w'] = struct.unpack('<f', pval)[0]
                            if pfn == 1009 and pwt == 'bytes':
                                sub = parse_all_fields(pval)
                                for sfn, swt, sval in sub:
                                    if sfn == 5 and swt == 'bytes' and len(sval) == 4:
                                        state['soc'] = struct.unpack('<f', sval)[0]
    except:
        pass

    batt = f"batt={state['batt_w']:.0f}W" if state['batt_w'] else "batt=?"
    soc = f"soc={state['soc']:.0f}%" if state['soc'] else ""
    print(f"  [{state['msg_count']:3d}] {dev} {batt} {soc}")

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected")
        client.subscribe([(DATA_TOPIC_ESG, 0), (DATA_TOPIC_DPU, 0),
                          (SET_REPLY_ESG, 0), (SET_REPLY_DPU, 0)])
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
        "from": "HomeAssistant", "id": "999901234", "version": "1.1",
        "moduleType": 0, "operateType": "latestQuotas", "params": {}
    }), qos=1)

print("Capturing first packets from ESG and DPU for 20s...")
time.sleep(20)

print(f"\n{'='*70}")
print(f"SUMMARY: captured {len(state['esg_packets'])} ESG, {len(state['dpu_packets'])} DPU packets")
print(f"batt_w={state['batt_w']}, soc={state['soc']}")

# ── Now send cmdFunc=254/ci=17 (DPUX ConfigWrite format) to ESG ──
print(f"\n{'='*70}")
print("TEST: Send DPUX-style ConfigWrite (cf=254/ci=17) to ESG")
print("This is the format that WORKED for DPUX (with cfgUtcTime)")

ts = int(time.time())
# ConfigWrite pdata: cfgUtcTime (field 6) + foce_charge_hight analog (try field 33=cms_max_chg_soc)
# For SHP2, foce_charge_hight is ProtoPushAndSet.field_6
# Let's also try the ESG-specific field: cms_max_chg_soc=33 which worked on DPUX

seq = int(time.time() * 1000) & 0xFFFFFFFF

# Test 1: ESG with same DPUX format (cf=254, ci=17, dest=11, version=4)
print("\n>>> CF=254/CI=17 DPUX-style ConfigWrite targeting ESG dest=11 v4")
pdata = pb_varint(6, ts)  # cfgUtcTime only
header = (
    pb_bytes(1, pdata) +
    pb_varint(2, 32) +   # src=32 app
    pb_varint(3, 11) +   # dest=11 ESG
    pb_varint(4, 1) +
    pb_varint(5, 1) +
    pb_varint(8, 254) +  # cmd_func=254
    pb_varint(9, 17) +   # cmd_id=17 ConfigWrite
    pb_varint(10, len(pdata)) +
    pb_varint(11, 1) +   # need_ack=1
    pb_varint(14, seq) +
    pb_varint(16, 4) +   # version=4
    pb_varint(17, 1) +
    pb_string(23, 'Android')
)
cmd = pb_bytes(1, header)
print(f"  Hex: {cmd.hex()}")
rc = client.publish(SET_TOPIC_ESG, cmd, qos=0)
print(f"  Published rc={rc.rc}")
time.sleep(8)
print(f"  Replies so far: {len(state['replies'])}")

# Test 2: ESG with DPUX format but version=3 (DPUX native version)
print("\n>>> CF=254/CI=17 DPUX-style ConfigWrite targeting ESG dest=11 v3")
ts = int(time.time()); seq = int(time.time() * 1000) & 0xFFFFFFFF
pdata = pb_varint(6, ts)
header = (
    pb_bytes(1, pdata) + pb_varint(2, 32) + pb_varint(3, 11) +
    pb_varint(4, 1) + pb_varint(5, 1) + pb_varint(8, 254) + pb_varint(9, 17) +
    pb_varint(10, len(pdata)) + pb_varint(11, 1) + pb_varint(14, seq) +
    pb_varint(16, 3) + pb_varint(17, 1) + pb_string(23, 'Android')
)
cmd = pb_bytes(1, header)
rc = client.publish(SET_TOPIC_ESG, cmd, qos=0)
print(f"  Published rc={rc.rc}")
time.sleep(8)
print(f"  Replies so far: {len(state['replies'])}")

# Test 3: ESG with JSON format on consumer MQTT (PD303_APP_SET)
print("\n>>> JSON PD303_APP_SET on consumer MQTT to ESG")
json_cmd = json.dumps({
    "sn": SN_ESG,
    "cmdCode": "PD303_APP_SET",
    "params": {"chargeWattPower": 500}
})
print(f"  JSON: {json_cmd}")
rc = client.publish(SET_TOPIC_ESG, json_cmd, qos=0)
print(f"  Published rc={rc.rc}")
time.sleep(8)
print(f"  Replies so far: {len(state['replies'])}")

# Test 4: Raw version=19 like SHP2 sim (createMsgFromObjects default)
print("\n>>> CF=12/CI=32 ESG ProtoPushAndSet with version=19 (SHP2 sim default)")
ts = int(time.time()); seq = int(time.time() * 1000) & 0xFFFFFFFF
pdata = pb_varint(64, ts) + pb_varint(6, 0)  # localTime + foce_charge_hight=0
header = (
    pb_bytes(1, pdata) + pb_varint(2, 32) + pb_varint(3, 11) +
    pb_varint(4, 1) + pb_varint(5, 1) + pb_varint(8, 12) + pb_varint(9, 32) +
    pb_varint(10, len(pdata)) + pb_varint(11, 1) + pb_varint(14, seq) +
    pb_varint(16, 19) + pb_varint(17, 1) + pb_string(23, 'Android')
)
cmd = pb_bytes(1, header)
rc = client.publish(SET_TOPIC_ESG, cmd, qos=0)
print(f"  Published rc={rc.rc}")
time.sleep(8)
print(f"  Replies so far: {len(state['replies'])}")

# Test 5: CF=12/CI=32 with dest=2 (same as DPUX) on ESG
print("\n>>> CF=12/CI=32 ESG ProtoPushAndSet with dest=2 (DPUX's dest)")
ts = int(time.time()); seq = int(time.time() * 1000) & 0xFFFFFFFF
pdata = pb_varint(64, ts) + pb_varint(6, 0)
header = (
    pb_bytes(1, pdata) + pb_varint(2, 32) + pb_varint(3, 2) +
    pb_varint(4, 1) + pb_varint(5, 1) + pb_varint(8, 12) + pb_varint(9, 32) +
    pb_varint(10, len(pdata)) + pb_varint(11, 1) + pb_varint(14, seq) +
    pb_varint(16, 4) + pb_varint(17, 1) + pb_string(23, 'Android')
)
cmd = pb_bytes(1, header)
rc = client.publish(SET_TOPIC_ESG, cmd, qos=0)
print(f"  Published rc={rc.rc}")
time.sleep(8)
print(f"  Replies so far: {len(state['replies'])}")

print(f"\n{'='*70}")
print(f"FINAL: batt_w={state['batt_w']}, soc={state['soc']}")
print(f"Total replies: {len(state['replies'])}")
for r in state['replies']:
    print(f"  {r}")
print(f"Total MQTT messages: {state['msg_count']}")
print(f"{'='*70}")

client.loop_stop()
client.disconnect()
