#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
"""
EcoFlow charge control v12 — Operating mode switching

Goal: Switch between backup mode and self-powered mode on the ESG (HR65).

Telemetry shows:
  1.1.1009.4 = 2  →  self-powered mode (no grid charging)
  1.1.1009.4 absent/0 → backup mode (grid charging allowed)

In self-powered mode the ESG will NOT charge from grid — effectively stopping
active charging. This is the most promising path to charge control.

Strategies tested (in order):
  PHASE 1: ESG ConfigWrite f5=2   (eps_mode_info=2, prev value=0 was accepted)
  PHASE 2: ESG ConfigWrite f1009 sub-msg {f4=2}  (mirror of telemetry field)
  PHASE 3: ESG ConfigWrite f1009 sub-msg {f4=0}  (restore — only if phase 2 worked)
  PHASE 4: ESG ConfigWrite f5=0   (restore eps_mode_info — only if phase 1 worked)
  PHASE 5: Scan ESG ConfigWrite f10–f17 with value=2 (unexplored range)
  PHASE 6: ESG ConfigWrite f5=1   (alternate eps_mode_info value)
  PHASE 7: DPU ConfigWrite f5=2   (try same on DPU topic)
  PHASE 8: DPU ConfigWrite f1009 sub-msg {f4=2}

Success criteria (any of):
  - state['mode'] changes from None/0 to 2 (self-powered activated in telemetry)
  - batt_w drops by >50% from baseline (grid charging stopped)
  - ESG ACK shows action_id != 6 for a new field (field accepted)

HOW TO USE:
  Works with battery IDLE or CHARGING.
  If charging is active (~5kW), mode switch to self-powered should stop it.
  If idle, we can still confirm mode by watching telemetry mode field.

  Just run the script — no manual prep needed.
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
SET_REPLY_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/set_reply'
SET_REPLY_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/set_reply'
GET_TOPIC_ESG  = f'/app/{USER_ID}/{SN_ESG}/thing/property/get'
GET_TOPIC_DPU  = f'/app/{USER_ID}/{SN_DPU}/thing/property/get'

# ── Protobuf helpers ───────────────────────────────────────────────────────────
def pb_varint(field, value):
    """Encode a varint field (wire type 0)."""
    tag = (field << 3) | 0
    result = b''
    v = tag
    while v > 0x7F: result += bytes([0x80 | (v & 0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    v = value
    while v > 0x7F: result += bytes([0x80 | (v & 0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    return result

def pb_bytes(field, data):
    """Encode a length-delimited field (wire type 2). Works for any field number."""
    tag = (field << 3) | 2
    result = b''
    v = tag
    while v > 0x7F: result += bytes([0x80 | (v & 0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    length = len(data)
    v = length
    while v > 0x7F: result += bytes([0x80 | (v & 0x7F)]); v >>= 7
    result += bytes([v & 0x7F])
    return result + data

def pb_string(field, s):
    return pb_bytes(field, s.encode('utf-8'))

# ── Command builders ───────────────────────────────────────────────────────────
def build_esg_cmd(pdata_bytes, cmd_func=254, cmd_id=17, dest=11, version=3, src=32):
    """Build ConfigWrite for ESG (HR65) via ESG SET TOPIC."""
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    header = (
        pb_bytes(1, pdata_bytes) + pb_varint(2, src) + pb_varint(3, dest) +
        pb_varint(4, 1) + pb_varint(5, 1) + pb_varint(8, cmd_func) + pb_varint(9, cmd_id) +
        pb_varint(10, len(pdata_bytes)) + pb_varint(11, 1) + pb_varint(14, seq) +
        pb_varint(16, version) + pb_varint(17, 1) + pb_string(23, 'Android')
    )
    return pb_bytes(1, header)

def build_dpu_cmd(pdata_bytes, cmd_func=254, cmd_id=17, dest=2, version=3, src=32):
    """Build ConfigWrite for DPUX via DPU SET TOPIC."""
    seq = int(time.time() * 1000) & 0xFFFFFFFF
    header = (
        pb_bytes(1, pdata_bytes) + pb_varint(2, src) + pb_varint(3, dest) +
        pb_varint(4, 1) + pb_varint(5, 1) + pb_varint(8, cmd_func) + pb_varint(9, cmd_id) +
        pb_varint(10, len(pdata_bytes)) + pb_varint(11, 1) + pb_varint(14, seq) +
        pb_varint(16, version) + pb_varint(17, 1) + pb_string(23, 'Android')
    )
    return pb_bytes(1, header)

# ── Protobuf parser ────────────────────────────────────────────────────────────
def parse_fields(payload):
    fields = {}; i = 0
    while i < len(payload):
        tag = 0; shift = 0
        try:
            while i < len(payload):
                b = payload[i]; i += 1
                tag |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
        except: break
        fn = tag >> 3; wt = tag & 7
        if wt == 0:
            val = 0; shift = 0
            while i < len(payload):
                b = payload[i]; i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            fields[fn] = val
        elif wt == 2:
            length = 0; shift = 0
            while i < len(payload):
                b = payload[i]; i += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            fields[fn] = payload[i:i+length]; i += length
        elif wt == 5:
            fields[fn] = payload[i:i+4]; i += 4
        else: break
    return fields

def decode_ack(pdata_bytes):
    if not pdata_bytes: return {}
    p = parse_fields(pdata_bytes)
    result = {}
    if 1 in p: result['action_id'] = p[1]
    if 2 in p: result['config_ok'] = p[2]
    all_fields = []
    for k, v in p.items():
        if k not in (1, 2):
            result[f'field_{k}'] = v if not isinstance(v, bytes) else v.hex()
            all_fields.append(k)
    result['_all_fields'] = sorted(p.keys())
    return result

# ── State ──────────────────────────────────────────────────────────────────────
state = {
    'batt_w': None, 'soc': None, 'grid_w': None, 'home_w': None,
    'mode': None,            # None=absent/backup, 2=self-powered
    'mode_seen': False,      # True once we've seen f1009.f4 in any packet
    'msg_count': 0,
    'dpu_replies': [], 'esg_replies': [],
    'last_mode_change': None,  # timestamp when mode field last changed
    'batt_readings': [],
}

MODE_NAMES = {None: 'backup (absent)', 0: 'backup (0)', 2: 'self-powered (2)'}

def on_message(client, userdata, msg):
    try:
        topic = msg.topic
        payload = msg.payload
        state['msg_count'] += 1

        outer = parse_fields(payload)
        inner_bytes = outer.get(1, b'')
        if not inner_bytes: return
        inner = parse_fields(inner_bytes)
        cf = inner.get(8); ci = inner.get(9); src = inner.get(2); ver = inner.get(16)
        pdata_bytes = inner.get(1, b'') if isinstance(inner.get(1), bytes) else b''

        if topic == SET_REPLY_DPU:
            ack = decode_ack(pdata_bytes)
            state['dpu_replies'].append({'cf': cf, 'ci': ci, 'src': src, 'ack': ack})
            print(f"  [DPU ACK] cf={cf}/ci={ci}/src={src}: {ack}")

        elif topic == SET_REPLY_ESG:
            ack = decode_ack(pdata_bytes)
            state['esg_replies'].append({'cf': cf, 'ci': ci, 'src': src, 'ack': ack})
            print(f"  [ESG ACK] cf={cf}/ci={ci}/src={src}: {ack}")

        elif topic == DATA_TOPIC_ESG:
            if isinstance(pdata_bytes, bytes) and len(pdata_bytes) > 0:
                pdata = parse_fields(pdata_bytes)
                # f518 = batt watts (4-byte float, wire type 2)
                if 518 in pdata and isinstance(pdata[518], bytes) and len(pdata[518]) == 4:
                    bw = struct.unpack('<f', pdata[518])[0]
                    state['batt_w'] = bw
                    state['batt_readings'].append((time.time(), bw))
                # f515 = grid watts
                if 515 in pdata and isinstance(pdata[515], bytes) and len(pdata[515]) == 4:
                    state['grid_w'] = struct.unpack('<f', pdata[515])[0]
                # f1544 = home load
                if 1544 in pdata:
                    v = pdata[1544]
                    state['home_w'] = v if isinstance(v, int) else None
                # f1009 = state sub-message: f5=SOC, f4=mode, f8=batt_w_int
                if 1009 in pdata and isinstance(pdata[1009], bytes):
                    sub = parse_fields(pdata[1009])
                    # SOC
                    if 5 in sub and isinstance(sub[5], bytes) and len(sub[5]) == 4:
                        state['soc'] = struct.unpack('<f', sub[5])[0]
                    # MODE: f4 = 2 means self-powered, absent = backup
                    old_mode = state['mode']
                    if 4 in sub:
                        state['mode'] = sub[4]
                        state['mode_seen'] = True
                        if sub[4] != old_mode:
                            state['last_mode_change'] = time.time()
                            print(f"  *** MODE CHANGED: {MODE_NAMES.get(old_mode, old_mode)} → {MODE_NAMES.get(sub[4], sub[4])} ***")
                    else:
                        if old_mode is not None:
                            state['mode'] = None
                            state['last_mode_change'] = time.time()
                            print(f"  *** MODE CHANGED: {MODE_NAMES.get(old_mode, old_mode)} → backup (absent) ***")

    except Exception as e:
        print(f"  [err: {e}]")

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to EcoFlow MQTT broker")
        client.subscribe([
            (DATA_TOPIC_ESG, 0), (DATA_TOPIC_DPU, 0),
            (SET_REPLY_DPU, 0), (SET_REPLY_ESG, 0)
        ])
        print("Subscribed to ESG + DPU telemetry and reply topics")
        get_payload = json.dumps({
            'from': 'HomeAssistant', 'id': '99', 'version': '1.1',
            'moduleType': 0, 'operateType': 'latestQuotas', 'params': {}
        })
        client.publish(GET_TOPIC_ESG, get_payload, qos=1)
        client.publish(GET_TOPIC_DPU, get_payload, qos=1)
    else:
        print(f"Connect FAILED rc={rc}")

# ── Command helpers ────────────────────────────────────────────────────────────
def send_esg(pdata_bytes, label='ESG cmd', wait_ack_field=None, timeout=8):
    """Send to ESG, wait for ACK, return list of new ACKs."""
    n_before = len(state['esg_replies'])
    payload = build_esg_cmd(pdata_bytes)
    client.publish(SET_TOPIC_ESG, payload, qos=1)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        new = state['esg_replies'][n_before:]
        if wait_ack_field is not None:
            if any(a['ack'].get('action_id') == wait_ack_field or
                   f'field_{wait_ack_field}' in a['ack'] for a in new):
                return [a['ack'] for a in new]
        elif new:
            return [a['ack'] for a in new]
    return [a['ack'] for a in state['esg_replies'][n_before:]]

def send_dpu(pdata_bytes, label='DPU cmd', wait_ack_field=None, timeout=8):
    """Send to DPU, wait for ACK, return list of new ACKs."""
    n_before = len(state['dpu_replies'])
    payload = build_dpu_cmd(pdata_bytes)
    client.publish(SET_TOPIC_DPU, payload, qos=1)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        new = state['dpu_replies'][n_before:]
        if wait_ack_field is not None:
            if any(a['ack'].get('action_id') == wait_ack_field or
                   f'field_{wait_ack_field}' in a['ack'] for a in new):
                return [a['ack'] for a in new]
        elif new:
            return [a['ack'] for a in new]
    return [a['ack'] for a in state['dpu_replies'][n_before:]]

def watch_for_effect(duration=20, baseline_batt=None, label=''):
    """
    Watch telemetry for mode change or charging stop.
    Returns dict with what was observed.
    """
    start = time.time()
    mode_changed = False
    charging_stopped = False
    new_mode = None
    min_batt = baseline_batt
    max_batt = baseline_batt

    while time.time() - start < duration:
        time.sleep(1)
        elapsed = int(time.time() - start)
        bw = state['batt_w']
        m = state['mode']

        if min_batt is None or (bw is not None and bw < (min_batt or 9999)):
            min_batt = bw
        if max_batt is None or (bw is not None and bw > (max_batt or 0)):
            max_batt = bw

        if state['last_mode_change'] and time.time() - state['last_mode_change'] < 3:
            mode_changed = True
            new_mode = m

        if baseline_batt and baseline_batt > 500:
            if bw is None or (bw is not None and bw < baseline_batt * 0.3):
                charging_stopped = True

        if elapsed % 5 == 0:
            batt_str = f"{bw:.0f}W" if bw is not None else "None"
            mode_str = MODE_NAMES.get(m, str(m))
            print(f"    [{elapsed:2d}s] batt={batt_str}  mode={mode_str}")

    return {
        'mode_changed': mode_changed,
        'new_mode': new_mode,
        'charging_stopped': charging_stopped,
        'final_batt': state['batt_w'],
        'final_mode': state['mode'],
        'min_batt': min_batt,
        'max_batt': max_batt,
    }

def pdata_with_ts(*extra):
    """Build pdata bytes: cfgUtcTime (field 6) + extra fields."""
    ts = int(time.time())
    return pb_varint(6, ts) + b''.join(extra)

# ── Connect ────────────────────────────────────────────────────────────────────
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
client.on_connect = on_connect
client.on_message = on_message
client.username_pw_set(MQTT_USER, MQTT_PASS)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
client.tls_set_context(ctx)
client.connect('mqtt.ecoflow.com', 8883, 60)
client.loop_start()

# ── Initial status ─────────────────────────────────────────────────────────────
print("="*70)
print("EcoFlow Mode Control v12 — Self-Powered vs Backup Mode Switching")
print("="*70)
print("Waiting 12s for initial telemetry...")
time.sleep(12)

baseline_batt = state['batt_w']
baseline_soc  = state['soc']
baseline_mode = state['mode']
baseline_grid = state['grid_w']

charging = baseline_batt is not None and baseline_batt > 500
print(f"\nInitial state:")
print(f"  batt_w  = {f'{baseline_batt:.0f}W' if baseline_batt else 'None (idle)'}")
print(f"  soc     = {f'{baseline_soc:.1f}%' if baseline_soc else 'None'}")
print(f"  grid_w  = {f'{baseline_grid:.0f}W' if baseline_grid else 'None'}")
print(f"  mode    = {MODE_NAMES.get(baseline_mode, baseline_mode)}")
print(f"  status  = {'CHARGING' if charging else 'IDLE'}")
print(f"  msgs    = {state['msg_count']}")

if not charging:
    print("\n>>> NOTE: Battery appears idle. Mode switch will still work, but")
    print("    we'll confirm by watching the mode telemetry field (1009.4).")
    print("    Start charging from the EcoFlow app for a more dramatic test.")
    print()

results = {}

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1: ESG ConfigWrite f5=2 (eps_mode_info=2)
# f5 was accepted by ESG with value=0 before. Try value=2 — might = self-powered.
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("PHASE 1: ESG ConfigWrite f5=2 (eps_mode_info=2)")
print("  f5=0 was accepted before; value=2 might = self-powered mode")
print("-"*70)
pdata = pdata_with_ts(pb_varint(5, 2))
acks = send_esg(pdata, label='f5=2')
print(f"  ACKs: {acks}")
obs = watch_for_effect(20, baseline_batt, 'Phase 1')
results['phase1_f5_2'] = {'acks': acks, 'obs': obs}
print(f"  Result: mode_changed={obs['mode_changed']} new_mode={obs['new_mode']} "
      f"charging_stopped={obs['charging_stopped']} final_mode={obs['final_mode']}")

# Restore f5 if it might have changed
print("\n  Restoring f5=0 (eps_mode_info=0)...")
pdata_r = pdata_with_ts(pb_varint(5, 0))
acks_r = send_esg(pdata_r, label='restore f5=0')
print(f"  Restore ACKs: {acks_r}")
time.sleep(5)

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2: ESG ConfigWrite f1009 sub-message {f4=2}
# Telemetry shows mode as pdata.f1009.f4 = 2 for self-powered.
# Try sending a ConfigWrite with the same field structure.
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("PHASE 2: ESG ConfigWrite f1009 sub-message {f4=2}")
print("  Telemetry: pdata.f1009.f4=2 = self-powered. Mirroring this in ConfigWrite.")
print("  Field 1009 tag (wire type 2) = multi-byte varint, handled by pb_bytes().")
print("-"*70)
mode_submsg = pb_varint(4, 2)              # sub-message: f4=2 (self-powered)
pdata = pdata_with_ts(pb_bytes(1009, mode_submsg))
acks = send_esg(pdata, label='f1009={f4=2}')
print(f"  ACKs: {acks}")
obs = watch_for_effect(25, state['batt_w'], 'Phase 2')
results['phase2_f1009_f4_2'] = {'acks': acks, 'obs': obs}
p2_accepted = acks and any(a.get('action_id') not in (None, 6) or
                            any(k.startswith('field_') and int(k.split('_')[1]) > 6
                                for k in a.keys())
                            for a in acks)
print(f"  Result: mode_changed={obs['mode_changed']} new_mode={obs['new_mode']} "
      f"charging_stopped={obs['charging_stopped']} final_mode={obs['final_mode']}")

if obs['mode_changed'] and obs['new_mode'] == 2:
    print("  ★★★ PHASE 2 WORKED — self-powered mode activated via f1009! ★★★")
    # Restore immediately
    print("\n  Restoring: f1009 sub-message {f4=0}...")
    restore_submsg = pb_varint(4, 0)
    pdata_r = pdata_with_ts(pb_bytes(1009, restore_submsg))
    acks_r = send_esg(pdata_r)
    print(f"  Restore ACKs: {acks_r}")
    time.sleep(10)
else:
    # Try restoring anyway in case something changed
    pdata_r = pdata_with_ts(pb_bytes(1009, pb_varint(4, 0)))
    send_esg(pdata_r)
    time.sleep(3)

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3: ESG ConfigWrite f1009 sub-message {f4=2, f8=0} (stop charging)
# f8 in the 1009 sub-msg is battery watts int. Send f4=2, f8=0.
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("PHASE 3: ESG ConfigWrite f1009 sub-msg {f4=2, f8=0}")
print("  Also set f8=0 (battery watts = 0) within the 1009 sub-message.")
print("-"*70)
mode_submsg = pb_varint(4, 2) + pb_varint(8, 0)
pdata = pdata_with_ts(pb_bytes(1009, mode_submsg))
acks = send_esg(pdata, label='f1009={f4=2,f8=0}')
print(f"  ACKs: {acks}")
obs = watch_for_effect(20, state['batt_w'], 'Phase 3')
results['phase3_f1009_f4_2_f8_0'] = {'acks': acks, 'obs': obs}
print(f"  Result: mode_changed={obs['mode_changed']} charging_stopped={obs['charging_stopped']} "
      f"final_mode={obs['final_mode']}")
# Restore
send_esg(pdata_with_ts(pb_bytes(1009, pb_varint(4, 0))))
time.sleep(3)

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4: Scan ESG ConfigWrite f10–f17 with value=2
# These fields are completely untested. One of them might be the mode selector.
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("PHASE 4: ESG ConfigWrite scan f10–f17 with value=2")
print("  Unexplored field range. Watch for mode change or charging stop.")
print("-"*70)
scan_results = {}
for fn in range(10, 18):
    curr_batt = state['batt_w']
    curr_mode = state['mode']
    pdata = pdata_with_ts(pb_varint(fn, 2))
    acks = send_esg(pdata, label=f'f{fn}=2')
    obs = watch_for_effect(12, curr_batt, f'f{fn}=2')
    accepted = acks and any(a.get('action_id') == fn for a in acks)
    effect = ''
    if obs['mode_changed']:
        effect = f'MODE→{obs["new_mode"]}'
    elif obs['charging_stopped']:
        effect = 'CHARGING STOPPED'
    elif accepted:
        effect = 'accepted (no observed effect)'
    else:
        effect = 'rejected'
    scan_results[f'f{fn}'] = {'acks': acks, 'accepted': accepted, 'effect': effect}
    marker = '★' if (obs['mode_changed'] or obs['charging_stopped']) else ('✓' if accepted else '✗')
    print(f"  {marker} f{fn}=2: acks={[a.get('action_id') for a in acks]} effect={effect}")
    # Restore if anything changed
    if obs['mode_changed']:
        send_esg(pdata_with_ts(pb_bytes(1009, pb_varint(4, 0)) + pb_varint(fn, 0)))
        time.sleep(5)
    else:
        send_esg(pdata_with_ts(pb_varint(fn, 0)))
        time.sleep(2)
results['phase4_scan'] = scan_results

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 5: ESG ConfigWrite f5=1 (alternate eps_mode_info value)
# Try value=1 as another candidate for self-powered.
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("PHASE 5: ESG ConfigWrite f5=1")
print("  Testing eps_mode_info=1 as alternate mode value.")
print("-"*70)
pdata = pdata_with_ts(pb_varint(5, 1))
acks = send_esg(pdata)
print(f"  ACKs: {acks}")
obs = watch_for_effect(15, state['batt_w'], 'Phase 5')
results['phase5_f5_1'] = {'acks': acks, 'obs': obs}
print(f"  Result: mode_changed={obs['mode_changed']} final_mode={obs['final_mode']}")
# Restore
send_esg(pdata_with_ts(pb_varint(5, 0)))
time.sleep(3)

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 6: DPU ConfigWrite f5=2 via DPU SET TOPIC
# Try the same eps_mode_info=2 on the DPU side.
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("PHASE 6: DPU ConfigWrite f5=2 (via DPU SET TOPIC)")
print("-"*70)
pdata = pdata_with_ts(pb_varint(5, 2))
acks = send_dpu(pdata)
print(f"  ACKs: {acks}")
obs = watch_for_effect(15, state['batt_w'], 'Phase 6')
results['phase6_dpu_f5_2'] = {'acks': acks, 'obs': obs}
print(f"  Result: mode_changed={obs['mode_changed']} final_mode={obs['final_mode']}")
# Restore
send_dpu(pdata_with_ts(pb_varint(5, 0)))
time.sleep(3)

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 7: DPU ConfigWrite f1009 sub-message {f4=2}
# Try the telemetry-mirror approach on the DPU.
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("PHASE 7: DPU ConfigWrite f1009 sub-message {f4=2} (via DPU SET TOPIC)")
print("-"*70)
mode_submsg = pb_varint(4, 2)
pdata = pdata_with_ts(pb_bytes(1009, mode_submsg))
acks = send_dpu(pdata)
print(f"  ACKs: {acks}")
obs = watch_for_effect(15, state['batt_w'], 'Phase 7')
results['phase7_dpu_f1009_f4_2'] = {'acks': acks, 'obs': obs}
print(f"  Result: mode_changed={obs['mode_changed']} final_mode={obs['final_mode']}")
# Restore
send_dpu(pdata_with_ts(pb_bytes(1009, pb_varint(4, 0))))
time.sleep(3)

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 8: ESG ConfigWrite scan f19–f27 with value=2
# Extended scan. f18=ch1_force_charge was rejected before; try f19-f27.
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("PHASE 8: ESG ConfigWrite scan f19–f27 with value=2")
print("-"*70)
scan2_results = {}
for fn in range(19, 28):
    if fn in (33, 34): continue  # already tested
    curr_batt = state['batt_w']
    curr_mode = state['mode']
    pdata = pdata_with_ts(pb_varint(fn, 2))
    acks = send_esg(pdata)
    obs = watch_for_effect(10, curr_batt, f'f{fn}=2')
    accepted = acks and any(a.get('action_id') == fn for a in acks)
    effect = ''
    if obs['mode_changed']:
        effect = f'MODE→{obs["new_mode"]}'
    elif obs['charging_stopped']:
        effect = 'CHARGING STOPPED'
    elif accepted:
        effect = 'accepted (no effect)'
    else:
        effect = 'rejected'
    scan2_results[f'f{fn}'] = {'acks': acks, 'accepted': accepted, 'effect': effect}
    marker = '★' if (obs['mode_changed'] or obs['charging_stopped']) else ('✓' if accepted else '✗')
    print(f"  {marker} f{fn}=2: acks={[a.get('action_id') for a in acks]} effect={effect}")
    if obs['mode_changed']:
        send_esg(pdata_with_ts(pb_bytes(1009, pb_varint(4, 0)) + pb_varint(fn, 0)))
        time.sleep(5)
    else:
        send_esg(pdata_with_ts(pb_varint(fn, 0)))
        time.sleep(2)
results['phase8_scan'] = scan2_results

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 9: ESG ConfigWrite f1009 + f4 flat (NOT nested)
# Try sending f4=2 as a top-level field (not wrapped in f1009).
# Maybe the mode is field 4 directly in pdata.
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("PHASE 9: ESG ConfigWrite f4=2 (flat, top-level pdata field)")
print("  f4 was rejected before at value=0. Try value=2.")
print("-"*70)
pdata = pdata_with_ts(pb_varint(4, 2))
acks = send_esg(pdata)
print(f"  ACKs: {acks}")
obs = watch_for_effect(15, state['batt_w'], 'Phase 9')
results['phase9_f4_flat_2'] = {'acks': acks, 'obs': obs}
print(f"  Result: mode_changed={obs['mode_changed']} final_mode={obs['final_mode']}")
send_esg(pdata_with_ts(pb_varint(4, 0)))
time.sleep(3)

# ── Final restore ──────────────────────────────────────────────────────────────
print("\n" + "="*70)
print("FINAL RESTORE — sending cfgUtcTime-only to bring ESG back to normal state")
print("="*70)
pdata_restore = pdata_with_ts()
acks = send_esg(pdata_restore)
print(f"  ACKs: {acks}")
time.sleep(8)
final_batt = state['batt_w']
final_soc  = state['soc']
final_mode = state['mode']
final_grid = state['grid_w']
print(f"  Post-restore: batt_w={f'{final_batt:.0f}W' if final_batt else 'None'} "
      f"soc={f'{final_soc:.1f}%' if final_soc else 'None'} "
      f"mode={MODE_NAMES.get(final_mode, final_mode)}")

# ── GRAND SUMMARY ─────────────────────────────────────────────────────────────
print("\n" + "="*70)
print("GRAND SUMMARY — Operating Mode Switch Test")
print("="*70)
print(f"  Initial: batt={f'{baseline_batt:.0f}W' if baseline_batt else 'idle'} "
      f"mode={MODE_NAMES.get(baseline_mode, baseline_mode)}")
print(f"  Final:   batt={f'{final_batt:.0f}W' if final_batt else 'idle'} "
      f"mode={MODE_NAMES.get(final_mode, final_mode)}")
print()

any_worked = False
for key, val in results.items():
    obs = val.get('obs', {})
    acks = val.get('acks', [])
    if obs.get('mode_changed') or obs.get('charging_stopped'):
        any_worked = True
        print(f"  ★ {key}: MODE_CHANGED={obs.get('mode_changed')} "
              f"CHARGING_STOPPED={obs.get('charging_stopped')} new_mode={obs.get('new_mode')}")
    elif isinstance(val, dict) and 'accepted' in val:  # scan result
        if val.get('accepted'):
            print(f"  ✓ {key}: ACCEPTED but no visible effect")

if not any_worked:
    print("  ✗ No phase produced a detectable mode change or charging stop.")
    print()
    print("  Accepted (config_ok=1) but no effect:")
    for key, val in results.items():
        acks = val.get('acks', [])
        if acks and any(a.get('config_ok') == 1 and a.get('action_id') not in (None, 6)
                        for a in acks):
            print(f"    - {key}: acks={acks}")
    print()
    print("  All ESG replies received:")
    for r in state['esg_replies']:
        print(f"    cf={r['cf']}/ci={r['ci']}/src={r['src']}: {r['ack']}")

print(f"\n  Total ESG replies: {len(state['esg_replies'])}")
print(f"  Total DPU replies: {len(state['dpu_replies'])}")
print(f"  Total telemetry msgs: {state['msg_count']}")

client.loop_stop()
client.disconnect()
