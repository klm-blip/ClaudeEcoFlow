"""
EcoFlow Token Signing Test
===========================
Tests the token header computation derived from jadx decompilation of:
  - an/a.java (Device Interceptor) - adds token/nonce/timestamp/did headers
  - cn/h.java (StringUtil) - HMAC-SHA256 computation
  - cn/h.g() - builds sign input from device params

Formula (from decompiled code):
  sign_input = "phoneModel={model}&platform=android&sysVersion={ver}&version={appVer}&nonce={N}&timestamp={T}"
  (params sorted alphabetically by key, nonce+timestamp appended last)
  hmac_key = LOGIN_TOKEN with "Bearer" removed and trimmed = raw JWT
  token = HMAC-SHA256(sign_input, hmac_key).hex().lowercase()
"""

import hmac
import hashlib
import time
import requests
import json
import os
import sys

# ─── Load credentials ────────────────────────────────────────────────
CRED_FILE = os.path.join(os.path.dirname(__file__), "ecoflow_credentials.txt")
creds = {}
with open(CRED_FILE) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip()

JWT = creds["REST_JWT"]
SN = "HR65ZA1AVH7J0027"

# ─── Device params (matching HTTP Toolkit capture) ───────────────────
PHONE_MODEL = "Pixel 10"
SYS_VERSION = "16"
APP_VERSION = "6.11.0.1731"
DID = "2f0b539d6db9f3eb78f6d9fb9d2c72c78"
PLATFORM = "android"

# ─── Nonce counter (starts at 100000, increments per request) ────────
_nonce_counter = 100000

def next_nonce():
    global _nonce_counter
    n = _nonce_counter
    _nonce_counter += 1
    if _nonce_counter >= 999999:
        _nonce_counter = 100000
    return n

def compute_token(nonce: int, timestamp: int, hmac_key: str) -> str:
    """
    Compute the 'token' header value.

    From cn/h.g():
    - Build sorted map of {phoneModel, platform, sysVersion, version}
    - Append nonce=N&timestamp=T
    - HMAC-SHA256 with the sign key
    """
    # Sorted alphabetically: phoneModel, platform, sysVersion, version
    sign_input = (
        f"phoneModel={PHONE_MODEL}&"
        f"platform={PLATFORM}&"
        f"sysVersion={SYS_VERSION}&"
        f"version={APP_VERSION}&"
        f"nonce={nonce}&"
        f"timestamp={timestamp}"
    )

    token = hmac.new(
        hmac_key.encode("utf-8"),
        sign_input.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    return token, sign_input

def make_signed_request(method: str, url: str, body: dict = None, hmac_key: str = None):
    """Make a request with proper token/nonce/timestamp/did headers."""
    nonce = next_nonce()
    timestamp = int(time.time() * 1000)

    token, sign_input = compute_token(nonce, timestamp, hmac_key)

    headers = {
        "Authorization": f"Bearer {JWT}",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
        "Connection": "Keep-Alive",
        "User-Agent": "okhttp/4.11.0",
        # Device headers (from an/a.java interceptor)
        "platform": PLATFORM,
        "version": APP_VERSION,
        "systemName": "user_app",
        "lang": "en-us",
        "countryCode": "US",
        "sysVersion": SYS_VERSION,
        "phoneModel": PHONE_MODEL,
        "nonce": str(nonce),
        "timestamp": str(timestamp),
        "token": token,
        "did": DID,
        "X-Appid": "-1",
    }

    print(f"\n{'='*60}")
    print(f"  {method} {url}")
    print(f"{'='*60}")
    print(f"  nonce:     {nonce}")
    print(f"  timestamp: {timestamp}")
    print(f"  token:     {token}")
    print(f"  sign_input: {sign_input}")

    if method == "POST":
        print(f"  body:      {json.dumps(body)}")
        resp = requests.post(url, json=body, headers=headers, timeout=15)
    else:
        resp = requests.get(url, headers=headers, timeout=15)

    print(f"\n  HTTP {resp.status_code}")
    try:
        data = resp.json()
        print(f"  Response: {json.dumps(data, indent=2)}")
    except:
        print(f"  Response: {resp.text[:500]}")

    return resp

# ─── Main test ───────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  EcoFlow Token Signing Test")
    print("=" * 60)
    print(f"JWT: {JWT[:40]}...")
    print(f"Phone: {PHONE_MODEL}, SysVer: {SYS_VERSION}, AppVer: {APP_VERSION}")
    print(f"DID: {DID}")

    # The HMAC key: from g(), it's aVar.n() with "Bearer" removed and trimmed
    # aVar.n() = LOGIN_TOKEN from SharedPreferences (stored as "Bearer <jwt>" or just "<jwt>")
    # After .replace("Bearer", "").trim() = raw JWT
    hmac_key = JWT  # The raw JWT (no "Bearer" prefix)

    print(f"\nHMAC key: JWT ({len(hmac_key)} chars)")

    # Test 1: Simple GET to verify headers are accepted
    print("\n\n[TEST 1] GET device status (verify signed headers work)")
    make_signed_request(
        "GET",
        f"https://api-a.ecoflow.com/iot-devices/device/status?sn={SN}",
        hmac_key=hmac_key
    )

    # Test 2: POST mode change to self-powered (targetMode=2)
    print("\n\n[TEST 2] POST notify-mode-changed (targetMode=2 = self-powered)")
    make_signed_request(
        "POST",
        "https://api-a.ecoflow.com/tou-service/goe/ai-mode/notify-mode-changed",
        body={"sn": SN, "systemNo": "", "targetMode": 2},
        hmac_key=hmac_key
    )

    print("\n  >>> Waiting 20 seconds for device to react...")
    print("  >>> Check the EcoFlow app NOW - does it switch to Self-Powered?")
    import time as _t
    for i in range(20, 0, -1):
        print(f"  ... {i}s remaining", end="\r")
        _t.sleep(1)
    print("  ... switching back to backup now")

    # Test 3: POST mode change back to backup (targetMode=-1)
    print("\n\n[TEST 3] POST notify-mode-changed (targetMode=-1 = backup)")
    make_signed_request(
        "POST",
        "https://api-a.ecoflow.com/tou-service/goe/ai-mode/notify-mode-changed",
        body={"sn": SN, "systemNo": "", "targetMode": -1},
        hmac_key=hmac_key
    )

    print("\n  >>> Waiting 20 seconds for device to react...")
    print("  >>> Check the EcoFlow app NOW - does it switch back to Backup?")
    for i in range(20, 0, -1):
        print(f"  ... {i}s remaining", end="\r")
        _t.sleep(1)

    print("\n\n" + "=" * 60)
    print("  DONE - Did the mode change in the EcoFlow app?")
    print("=" * 60)

    input("\nPress Enter to close...")
