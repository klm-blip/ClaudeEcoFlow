# Gateway Swap Checklist — April 6, 2026

## Pre-Swap (before the installer arrives)

- [ ] **Stop the dashboard on Pi**: `ssh pi@kpi.local` → `cd /home/pi/ecoflow && docker compose down`
- [ ] **Back up current credentials**: `cp /home/pi/automation_data/ecoflow_credentials.txt /home/pi/automation_data/ecoflow_credentials.txt.bak`
- [ ] **Note current gateway SN** for reference: `HR65ZA1AVH7J0027` (prototype)
- [ ] **Have HTTP Toolkit ready** on your laptop — install it and set up Android interception before the installer arrives so you're not scrambling

## During/After Physical Install

- [ ] **Record new gateway SN** — printed on the unit and visible in EcoFlow app after pairing
- [ ] **Pair new gateway in EcoFlow app** — follow EcoFlow's normal setup flow
- [ ] **Confirm DPUX re-pairs** — check that inverter SN `P101ZA1A9HA70164` still shows in the app
- [ ] **Note any additional device SNs** the app shows (there was an extra one before: `HR6AZA1AVH7HO056`)
- [ ] **Test basic control from the app** — switch modes, start/stop charge. Confirm the hardware responds normally before we touch our system.

## Capture New Credentials via HTTP Toolkit

### Setup
1. Open HTTP Toolkit on your laptop
2. Start intercepting your Android phone's traffic
3. Open the EcoFlow app on your phone (it fires credential requests on launch)

### What to Capture (you need ONE key request)

**MQTT Credentials — GET request:**
- **Filter** requests in HTTP Toolkit by `api-a.ecoflow.com`
- **Find:** `GET https://api-a.ecoflow.com/iot-auth/app/certification?userId=1971363830522871810`
- **In the response body**, you'll see:
```json
{
  "code": "0",
  "data": {
    "certificateAccount": "app-xxxxxxxxxxxxxxxxxxxx",   ← this is MQTT_USER
    "certificatePassword": "xxxxxxxxxxxxxxxxxxxxxxxx",  ← this is MQTT_PASS
    "url": "mqtt-a.ecoflow.com",                        ← broker host (confirm still the same)
    "port": "8883"
  }
}
```
- [ ] Copy `certificateAccount` → this is **MQTT_USER**
- [ ] Copy `certificatePassword` → this is **MQTT_PASS**
- [ ] Confirm `url` is still `mqtt-a.ecoflow.com`

**JWT Token — from the request headers:**
- On that same request (or any request to `api-a.ecoflow.com`), look at the **request headers**
- Find: `Authorization: Bearer eyJ...` (long base64 string)
- [ ] Copy the full token after `Bearer ` → this is **REST_JWT**

**Note:** MQTT credentials are tied to your EcoFlow account, not the device, so they *might* be the same as before. Capture them anyway to confirm.

### Summary of Values to Record
| Value | Where to Find | Example Format |
|-------|--------------|----------------|
| New Gateway SN | Printed on unit / EcoFlow app | `HR65XXXXXXXXXX` |
| MQTT_USER | Response body → `certificateAccount` | `app-740f41d44de0...` |
| MQTT_PASS | Response body → `certificatePassword` | `c1e46f17f699...` |
| REST_JWT | Request header → `Authorization: Bearer` | `eyJhbGci...` |
| Broker host | Response body → `url` | `mqtt-a.ecoflow.com` |
| Inverter SN | EcoFlow app device list | `P101ZA1A9HA70164` (should be same) |

## Update the System

### 1. Update credentials file on Pi
```bash
ssh pi@kpi.local
nano /home/pi/automation_data/ecoflow_credentials.txt
```
Update MQTT_USER, MQTT_PASS, and REST_JWT with new values. CLIENT_ID random number should be fine as-is.

### 2. Update gateway SN in code
On your desktop, open `ecoflow_web/config.py` and change line 14:
```python
GATEWAY_SN  = "NEW_SN_HERE"
```
That's the only code change needed — all MQTT topics derive from this.

### 3. Push and deploy
```bash
# On desktop
cd "C:\Users\kmars\OneDrive\Desktop\Claude EcoFlow Project"
git add ecoflow_web/config.py
git commit -m "Update gateway SN for production unit"
git push

# On Pi
ssh pi@kpi.local
cd /home/pi/ecoflow
git pull
docker compose up -d --build
```

## Verify Everything Works

### Test 1: MQTT Connection
```bash
docker compose logs -f dashboard
```
- [ ] Look for: `MQTT connect rc=0` (success)
- [ ] If `rc=5`: credentials wrong or client ID format rejected
- [ ] If `rc=7`: client ID collision (phone app using same random number — close the app)

### Test 2: Telemetry Flowing
- [ ] Open dashboard in browser (`http://kpi.local:5000` or Tailscale IP)
- [ ] SOC percentage populates (not `---%`)
- [ ] Grid watts, battery watts, load watts all showing real numbers
- [ ] Price data still flowing (this is independent of gateway — should work regardless)

### Test 3: Commands Work
- [ ] Switch to Backup mode from dashboard → confirm EcoFlow app agrees
- [ ] Switch to Self-Powered mode → confirm
- [ ] Start a charge at low rate → confirm charging indicator appears
- [ ] Stop charge → confirm
- [ ] Check command log on dashboard for ACK confirmations

### Test 4: Arbiter
```bash
docker compose logs -f arbiter
```
- [ ] Arbiter fetching state successfully (no connection errors)
- [ ] Arbiter showing SOC and price data in logs
- [ ] Decisions being logged (still in dry-run mode, so no harm)

## If Commands Don't Work

The production firmware might use slightly different protobuf fields. Signs:
- Telemetry flows fine but commands have no effect
- No ACK on set_reply topic after sending commands
- Different field numbers in telemetry payloads

**If this happens:**
1. Don't panic — the archive has all the reverse engineering tools
2. In HTTP Toolkit, switch a mode from the EcoFlow app and capture the MQTT publish payload
3. Compare the protobuf payloads to what we send (documented in `memory/protobuf_details.md`)
4. Start a Claude session — we can diff the old vs new commands and update `proto_codec.py`

## Post-Swap Cleanup

- [ ] Update `memory/MEMORY.md` with new gateway SN
- [ ] Delete the `.bak` credential file once everything is confirmed working
- [ ] Monitor for a full day — watch charge/discharge cycles, verify automation behaves normally
- [ ] Check energy tracking CSV is logging correctly
