# EcoFlow Smart Gateway (ESG) / Delta Pro Ultra X — MQTT & Protobuf Control Reference

> **Reverse-engineered from the EcoFlow Android app (APK decompilation via JADX) and live MQTT traffic capture (HTTP Toolkit).** Confirmed working as of March 2026 on Smart Gateway (HR65) + Delta Pro Ultra X (DPUX). Should also work for the Smart Home Panel 3 (SHP3) with little or no modification, though this has not been tested.

## Table of Contents
- [Overview](#overview)
- [Authentication](#authentication)
- [MQTT Connection](#mqtt-connection)
- [MQTT Topics](#mqtt-topics)
- [Protobuf Message Structure](#protobuf-message-structure)
- [Commands](#commands)
- [Telemetry](#telemetry)
- [Common Pitfalls](#common-pitfalls)
- [Tools Used](#tools-used)

---

## Overview

EcoFlow's newer devices (Smart Gateway, Smart Home Panel 3, Delta Pro Ultra X) use **MQTT over TLS** with **protobuf-encoded binary payloads** for device control. This is the same protocol the official EcoFlow mobile app uses.

The general flow:
1. **Authenticate** via REST API to get MQTT credentials
2. **Connect** to the MQTT broker
3. **Subscribe** to telemetry topics to receive device state
4. **Publish** protobuf-encoded commands to control the device

Prior-generation EcoFlow devices (Delta Pro, Delta Max, etc.) used simpler MQTT with JSON payloads. The Smart Gateway / SHP3 generation uses a different protobuf schema (`DevAplComm.ConfigWrite`) that was not previously documented.

---

## Authentication

### Step 1: REST Login
```
POST https://api-a.ecoflow.com/auth/login
Content-Type: application/json

{
  "email": "your@email.com",
  "password": "<encrypted_password>",
  "scene": "IOT_APP",
  "userType": "ECOFLOW"
}
```

**Password encryption** (reverse-engineered from APK):
```python
import hmac, hashlib

# Step 1: HMAC-SHA1 with EcoFlow's first key
step1 = hmac.new(
    b"moIHij9oU(*bik&^%&*imlYUTink$%E6fU#f278",
    raw_password.encode(),
    hashlib.sha1
).hexdigest()

# Step 2: HMAC-SHA256 with EcoFlow's second key
encrypted = hmac.new(
    b"zgJfBLaBKsOBXxjExFekYx8uZWOCCoDQHDWtGSgiRPUtAW4ARA8z7UlPYw0KIC5D",
    step1.encode(),
    hashlib.sha256
).hexdigest()
```

The login response returns a JWT token (valid ~30 days).

### Step 2: Get MQTT Credentials
```
GET https://api-a.ecoflow.com/iot-auth/app/certification?userId={userId}
Authorization: Bearer {jwt_token}
```

Response includes:
- `certificateAccount` → MQTT username
- `certificatePassword` → MQTT password
- `userId` → used in client ID and topics

These MQTT credentials are stable and tied to your account (they don't rotate frequently).

### Note on the Developer API
EcoFlow has a separate developer API program. However, developer credentials connect to a different topic namespace and **cannot access the app-style control topics**. The developer REST API also returns "device not allowed" for newer devices (Smart Gateway, DPUX). Use the app authentication path described above.

---

## MQTT Connection

| Parameter | Value |
|-----------|-------|
| **Broker** | `mqtt-a.ecoflow.com` |
| **Port** | `8883` (TLS) |
| **Protocol** | MQTTv3.1.1 |
| **Clean Session** | `True` (required) |
| **Client ID** | `ANDROID_{random_number}_{userId}` |

### Critical Connection Details

- **Use `mqtt-a.ecoflow.com`**, NOT `mqtt.ecoflow.com`. The non-`-a` host returns rc=5 (not authorized) for newer devices.
- **`clean_session=True` is required.** Without it, connection may succeed but subscriptions won't work reliably.
- **Client ID format is validated by the broker.** It must be exactly `ANDROID_{number}_{userId}`. Do not append suffixes, timestamps, or other identifiers — the broker will reject with rc=5.
- **Each client ID must be unique.** If two clients connect with the same random number, they will kick each other off in an rc=7 loop. Use a different random number for each application connecting simultaneously.

```python
import paho.mqtt.client as mqtt

client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION1,
    client_id=f"ANDROID_{random_number}_{user_id}",
    protocol=mqtt.MQTTv311,
    clean_session=True,
)
client.username_pw_set(mqtt_user, mqtt_pass)
client.tls_set()  # Uses system CA bundle for TLS
client.connect("mqtt-a.ecoflow.com", 8883)
```

---

## MQTT Topics

All topics use the device serial number (SN). The gateway SN starts with `HR65` (Smart Gateway) or `HR6A`.

| Purpose | Topic Pattern |
|---------|--------------|
| **Telemetry** (subscribe) | `/app/device/property/{SN}` |
| **Command** (publish) | `/app/{userId}/{SN}/thing/property/set` |
| **Command ACK** (subscribe) | `/app/{userId}/{SN}/thing/property/set_reply` |
| **Request telemetry** (publish) | `/app/{userId}/{SN}/thing/property/get` |

### Triggering Telemetry

Telemetry doesn't stream continuously. To trigger an initial burst of data, publish a `latestQuotas` GET request:

```python
import json

get_topic = f"/app/{user_id}/{sn}/thing/property/get"
payload = json.dumps({
    "header": {"src": "Android", "dest": "iOS", "from": "ios"},
    "cmdFunc": 254,
    "cmdId": 17,
    "dataLen": 0,
    "pdata": {},
    "id": "latestQuotas",
    "version": "1.0",
})
client.publish(get_topic, payload.encode(), qos=1)
```

Send this for both the gateway SN and the inverter SN to get complete system telemetry.

---

## Protobuf Message Structure

Commands are protobuf-encoded (not JSON). The wrapping structure is:

```
Send_Header_Msg (field 1)
  └─ Common.Header
       ├─ field 1: pdata (bytes) ← the actual ConfigWrite command
       ├─ field 2: src = 32
       ├─ field 3: dest = 11         ← IMPORTANT: 11 for Smart Gateway/SHP3
       ├─ field 4: dSrc = 1
       ├─ field 5: dDest = 1
       ├─ field 8: cmdFunc = 254
       ├─ field 9: cmdId = 17
       ├─ field 10: dataLen = len(pdata)
       ├─ field 11: needAck = 1
       ├─ field 14: seq (random int, used for ACK matching)
       ├─ field 15: productId = 1
       ├─ field 16: version = 19
       ├─ field 17: payloadVer = 1
       └─ field 23: "Android" (string)
```

### Key Detail: dest=11

The `dest` field in the header identifies the device class:
- `dest=11` → Smart Gateway / SHP3 (serial starts with "HR6")
- `dest=2` → Other device types (older generation)

This was determined from APK decompilation: the app checks if the SN starts with "HR5" (returns dest=2 via Y2 function) vs "HR6" (returns dest=11 via C0 function). Getting this wrong means your commands are silently ignored.

### Protobuf Encoding

Standard protobuf wire format. For manual encoding without a .proto file:

```python
def encode_varint(value):
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)

def encode_field_varint(field_num, value):
    tag = (field_num << 3) | 0  # wire type 0
    return encode_varint(tag) + encode_varint(value)

def encode_field_bytes(field_num, data):
    tag = (field_num << 3) | 2  # wire type 2
    return encode_varint(tag) + encode_varint(len(data)) + data
```

---

## Commands

All commands are `DevAplComm.ConfigWrite` messages (NOT `Yj751Common.ConfigWrite`, which is for other devices like PD303). The ConfigWrite message contains different fields depending on the command.

### Mode Switching — ConfigWrite field 544

`CfgPanelEnergyStrategyOperateMode`: Controls the operating mode.

| Field | Type | Purpose |
|-------|------|---------|
| 1 | bool | Self-Powered mode (battery powers home) |
| 2 | bool | Scheduled mode |
| 3 | bool | TOU (Time of Use) mode |
| 4 | varint | EPS mode (0=off, 1=on) — Emergency Power Supply |

**Mode combinations:**
| Desired Mode | Field 1 | Field 2 | Field 3 |
|-------------|---------|---------|---------|
| Self-Powered | true | false | false |
| Backup (grid powers home) | false | false | false |
| Scheduled | false | true | false |
| TOU | false | false | true |

**EPS mode** (field 4) can be sent alone to toggle emergency power without changing the operating mode. When EPS is enabled, the battery provides 20ms switchover power during grid outages.

### Grid Charging — ConfigWrite field 535

`BackupCtrl`: Controls grid-to-battery charging.

| Field | Type | Purpose |
|-------|------|---------|
| 1 | varint | ctrlEn = 1 (always set to 1) |
| 2 | varint | ctrlForceChg: 1=ON, **2=OFF** (not 0!) |
| 3 | varint | ctrlNormalChg: alternate charge control |

**Important:** Charge OFF is value `2`, not `0`. Sending `0` does not turn off charging.

The field number is `534 + channel` where channel is typically 1 (so field 535).

### Charge Power & Max SOC — ConfigWrite fields 542 + 33

These can be sent together in a single ConfigWrite message:

| Field | Type | Purpose |
|-------|------|---------|
| 542 | varint | `cfgPanelMaxChargePowSet` — charge rate in watts |
| 33 | varint | `cfgMaxChgSoc` — maximum charge SOC in percent |

### Full Command Example

To switch to self-powered mode:

```python
# 1. Build the ConfigWrite payload (field 544 with field 1 = true)
mode_inner = encode_field_varint(1, 1)  # self_powered = true
config_write = encode_field_bytes(544, mode_inner)

# 2. Wrap in Common.Header
seq = random.randint(100000, 999999)
header = b""
header += encode_field_bytes(1, config_write)  # pdata
header += encode_field_varint(2, 32)            # src
header += encode_field_varint(3, 11)            # dest (Smart Gateway)
header += encode_field_varint(4, 1)             # dSrc
header += encode_field_varint(5, 1)             # dDest
header += encode_field_varint(8, 254)           # cmdFunc
header += encode_field_varint(9, 17)            # cmdId
header += encode_field_varint(10, len(config_write))  # dataLen
header += encode_field_varint(11, 1)            # needAck
header += encode_field_varint(14, seq)          # seq
header += encode_field_varint(15, 1)            # productId
header += encode_field_varint(16, 19)           # version
header += encode_field_varint(17, 1)            # payloadVer

# 3. Wrap in Send_Header_Msg
message = encode_field_bytes(1, header)

# 4. Publish
client.publish(command_topic, message, qos=1)
```

---

## Telemetry

Telemetry messages arrive on `/app/device/property/{SN}` as protobuf-encoded bytes. Key fields observed (nested structure varies by device):

### Gateway (HR65) telemetry — selected fields
- Operating mode, EPS status
- Grid power (watts), voltage, frequency
- Circuit-level power readings

### Inverter (DPUX / P101) telemetry — selected fields
- Battery SOC (%)
- Battery power (watts) — positive = charging, negative = discharging
- Battery voltage, current, temperature
- Inverter output power
- Individual battery pack status

Telemetry field mappings depend on the specific firmware version. Use the protobuf decoder to explore the nested structure — the raw bytes decode into nested field→value maps. See `proto_codec.py` in the repository for a working decoder.

---

## Common Pitfalls

| Problem | Symptom | Solution |
|---------|---------|----------|
| Wrong MQTT broker | rc=5 on connect | Use `mqtt-a.ecoflow.com`, not `mqtt.ecoflow.com` |
| Client ID has suffix | rc=5 on connect | Must be exactly `ANDROID_{number}_{userId}`, nothing appended |
| `clean_session=False` | Subscriptions silently fail | Always use `clean_session=True` |
| Duplicate client ID | rc=7 loop (both clients kick each other) | Use unique random numbers in each client's ID |
| `dest=2` in header | Commands silently ignored | Use `dest=11` for Smart Gateway / SHP3 devices |
| Charge OFF = 0 | Charging doesn't stop | Charge OFF is value `2`, not `0` |
| Using `Yj751Common` proto | Commands ignored | Smart Gateway uses `DevAplComm.ConfigWrite` |
| REST API for control | No physical effect | REST endpoint is cloud notification only — use MQTT |
| Developer API credentials | rc=5 on app topics | Developer API is a separate namespace; use app auth |

---

## Tools Used

- **JADX** — Android APK decompiler. Used to find protobuf field numbers, HMAC keys, client ID format validation, and `dest` routing logic.
- **HTTP Toolkit** — MITM proxy for capturing MQTT-over-TLS traffic from the EcoFlow app. Used to observe real command payloads and telemetry structure.
- **Paho MQTT** (Python) — MQTT client library for connecting and publishing.
- **Custom protobuf encoder/decoder** — Since we don't have the .proto files, we encode/decode manually using the protobuf wire format. See `proto_codec.py` in the repository.

---

## Disclaimer

This documentation was produced through reverse engineering for personal home automation use. It is not affiliated with or endorsed by EcoFlow. Protocol details may change with firmware updates. Use at your own risk.
