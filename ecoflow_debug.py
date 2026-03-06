import os
import struct
import time

log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug.log")
log_f = open(log_path, "w", buffering=1)

def L(s=""):
    print(s)
    log_f.write(str(s) + "\n")
    log_f.flush()

L("step 1: file open OK")

try:
    import paho.mqtt.client as mqtt
    L("step 2: paho imported OK, version=" + str(getattr(mqtt, '__version__', 'unknown')))
except Exception as e:
    L("step 2 FAILED: " + str(e))
    input("Press Enter...")
    raise

L("step 3: loading credentials")
_dir = os.path.dirname(os.path.abspath(__file__))
_cred_file = os.path.join(_dir, "ecoflow_credentials.txt")
MQTT_USER = "app-740f41d44de04eaf83832f8a801252e9"
MQTT_PASS = "c1e46f17f6994a1e8252f1e1f3135b68"
CLIENT_ID = "ANDROID_666188426_1971363830522871810"
if os.path.exists(_cred_file):
    for line in open(_cred_file).read().splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        if "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "MQTT_USER": MQTT_USER = v
            if k == "MQTT_PASS": MQTT_PASS = v
            if k == "CLIENT_ID": CLIENT_ID = v
    L("credentials loaded from file")
else:
    L("credentials file not found, using defaults")
L("CLIENT_ID=" + CLIENT_ID)
L("MQTT_USER=" + MQTT_USER)

L("step 4: building MQTT client")
try:
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID)
        L("step 4: used VERSION1 API")
    except:
        c = mqtt.Client(client_id=CLIENT_ID)
        L("step 4: used legacy API")
except Exception as e:
    L("step 4 FAILED: " + str(e))
    input("Press Enter...")
    raise

L("step 5: setting credentials")
c.username_pw_set(MQTT_USER, MQTT_PASS)
c.tls_set()

msgs_received = [0]
fields_dump   = [None]

def on_connect(client, userdata, flags, rc):
    L("on_connect rc=" + str(rc))
    if rc == 0:
        client.subscribe("/app/device/property/HR65ZA1AVH7J0027", qos=1)
        client.subscribe("/app/device/property/P101ZA1A9HA70164", qos=1)
        L("subscribed to telemetry topics")

def on_message(client, userdata, msg):
    msgs_received[0] += 1
    n = msgs_received[0]
    L("msg #" + str(n) + " topic=" + msg.topic + " len=" + str(len(msg.payload)))
    if n == 1:
        L("  hex (first 80 bytes): " + msg.payload[:80].hex())
        # Try to decode fields
        try:
            fields = {}
            pos = 0
            data = msg.payload
            def dvi(d, p):
                r, s = 0, 0
                while p < len(d):
                    b = d[p]; p += 1
                    r |= (b & 0x7F) << s
                    if not (b & 0x80): break
                    s += 7
                return r, p
            def decode(d, pfx=""):
                p = 0
                while p < len(d):
                    try:
                        tag, p = dvi(d, p)
                        fn = tag >> 3; wt = tag & 7
                        if fn == 0: break
                        k = pfx + str(fn)
                        if wt == 0:
                            v, p = dvi(d, p)
                            fields[k] = v
                        elif wt == 2:
                            ln, p = dvi(d, p)
                            raw = d[p:p+ln]; p += ln
                            decode(raw, k + ".")
                        elif wt == 5:
                            v = struct.unpack_from('<f', d, p)[0]; p += 4
                            fields[k] = round(v, 2)
                        else:
                            break
                    except:
                        break
            decode(data)
            fields_dump[0] = fields
            L("  decoded fields:")
            for k in sorted(fields.keys(), key=str):
                v = fields[k]
                if isinstance(v, (int, float)):
                    L("    [" + str(k) + "] = " + str(v))
        except Exception as e:
            L("  decode error: " + str(e))

c.on_connect = on_connect
c.on_message = on_message

L("step 6: connecting...")
try:
    c.connect("mqtt.ecoflow.com", 8883, keepalive=60)
    c.loop_start()
except Exception as e:
    L("step 6 FAILED: " + str(e))
    input("Press Enter...")
    raise

L("waiting 20s for messages...")
for i in range(20):
    time.sleep(1)
    L("  t=" + str(i+1) + "s  msgs=" + str(msgs_received[0]))
    if msgs_received[0] >= 2:
        break

L("done. msgs=" + str(msgs_received[0]))
L("log: " + log_path)
c.loop_stop()
c.disconnect()
input("Press Enter to close...")
