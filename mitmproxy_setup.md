# Capture EcoFlow App HTTPS Traffic — Android

## Goal
Intercept HTTPS requests from the EcoFlow Android app to discover the exact
command format used to control the HR65 Smart Home Panel (start/stop charging,
change operating mode).

We've exhausted all known MQTT/REST approaches. mitmproxy will reveal what the
app sends to the EcoFlow cloud — specifically the `cmdCode` and params for HR65.

---

## Option A: HTTP Toolkit (RECOMMENDED — Easiest for Android)

HTTP Toolkit is purpose-built for Android HTTPS interception and handles
the certificate installation automatically via ADB.

### Install HTTP Toolkit on your laptop
Download from: https://httptoolkit.com/  (free tier is sufficient)

### Connect Android via USB

1. On Android: **Settings → Developer Options → Enable USB Debugging**
   - (Developer Options: Settings → About Phone → tap Build Number 7 times)
2. Plug phone into laptop via USB
3. Accept the RSA fingerprint prompt on the phone when it appears

### Set up HTTP Toolkit

1. Open HTTP Toolkit on laptop
2. Click **"Android Device via ADB"**
3. HTTP Toolkit installs its CA cert automatically and configures the proxy
4. The EcoFlow app traffic will now be intercepted

### Capture the traffic

1. In HTTP Toolkit, set filter to: `ecoflow` (top search bar)
2. Open the **EcoFlow app** on your phone
3. Do each action separately and watch the requests:
   - **Start charging** (e.g., set 3000W backup charge)
   - **Stop charging**
   - **Switch to Self-Powered mode**
   - **Switch back to Backup mode**

### Read the results

Click each EcoFlow request in HTTP Toolkit. In the **Request** tab look for:
- URL endpoint (e.g. `POST /iot-open/...` or `/app/...`)
- JSON body — specifically `cmdCode`, `params`, `operateType`

---

## Option B: mitmproxy (Manual)

Use this if you prefer mitmproxy or HTTP Toolkit doesn't work.

**⚠️ Android 7+ caveat**: Modern Android apps ignore user-installed CA certs.
You may need to try the ADB system-cert install below (Step 4b).

### Step 1: Install mitmproxy on laptop

```powershell
pip install mitmproxy
```

Or download from: https://mitmproxy.org/

### Step 2: Find your laptop's IP

```powershell
ipconfig
```
Look for **IPv4 Address** on your WiFi adapter (e.g., `192.168.1.150`).

### Step 3: Start mitmweb

```powershell
mitmweb --listen-port 8080
```
Web UI opens at http://127.0.0.1:8081

### Step 4a: Configure Android WiFi Proxy

1. **Settings → Wi-Fi → long press your network → Modify network**
2. Expand **Advanced options**
3. Set **Proxy → Manual**
4. Hostname: your laptop IP (e.g., `192.168.1.150`)
5. Port: `8080`
6. Save

### Step 4b: Install mitmproxy CA Certificate

**Method 1 — Browser install (works for Android ≤6, may fail on newer):**
1. On Android, open Chrome
2. Navigate to `http://mitm.it`
3. Tap **Android** → download the cert file
4. **Settings → Security → Install from storage** → select the downloaded cert
5. Name it "mitmproxy", type: **CA certificate**

**Method 2 — ADB system cert (required for Android 7+, needs USB debugging):**
```powershell
# Run from laptop with phone connected via USB
# Get the cert hash
openssl x509 -inform PEM -subject_hash_old -in %USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.pem

# Rename cert to hash.0 (replace XXXXXXXX with the hash output)
copy %USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.pem XXXXXXXX.0

# Push to Android system cert store (requires adb root)
adb root
adb shell mount -o rw,remount /system
adb push XXXXXXXX.0 /system/etc/security/cacerts/
adb shell chmod 644 /system/etc/security/cacerts/XXXXXXXX.0
adb reboot
```
Note: `adb root` only works on emulators or rooted devices.
For non-rooted phones, use **HTTP Toolkit** (Option A) instead.

### Step 5: Capture traffic in mitmweb

1. Open `http://127.0.0.1:8081` in browser
2. Filter: type `ecoflow` in the search bar
3. Use EcoFlow app on phone — do the mode switches and charge commands
4. Click each request to inspect it

### Step 6: Cleanup

- Remove proxy from Android WiFi settings when done
- Stop mitmproxy (`Ctrl+C`)

---

## If Certificate Pinning Blocks Interception

The EcoFlow app may use **certificate pinning** (refusing to work with our cert).
Signs: app shows "no internet" or requests don't appear in mitmweb at all.

### Bypass option: Frida + objection (advanced)

```powershell
pip install frida-tools objection
# Install frida-server on Android (requires ADB + root or special build)
# Then: objection -g com.ecoflow.elec explore --startup-command "android sslpinning disable"
```

### Easier bypass option: HTTP Toolkit Pro

HTTP Toolkit Pro ($14/mo) can patch the APK to disable pinning automatically.

---

## What to Look For in the Captured Requests

When you switch modes or change charging, look for a POST request to
something like:
```
POST https://api.ecoflow.com/iot-open/sign/device/quota
  OR
POST https://api.ecoflow.com/app/...
```

With body like:
```json
{
  "sn": "HR65ZA1AVH7J0027",
  "cmdCode": "???",       <-- THIS is what we need (NOT PD303_APP_SET)
  "params": {
    "???": ???            <-- AND these param names
  }
}
```

OR it might use a completely different endpoint/format.

**Copy the full URL + request body and share it** — that's all we need
to wire up the automation in `ecoflow_dashboard.py`.

---

## Quick Checklist

- [ ] HTTP Toolkit installed on laptop
- [ ] USB Debugging enabled on Android phone
- [ ] Phone connected to laptop via USB
- [ ] HTTP Toolkit shows "Android Device via ADB" connected
- [ ] EcoFlow app traffic visible in HTTP Toolkit
- [ ] Captured: mode switch request
- [ ] Captured: start charging request
- [ ] Captured: stop charging request
