"""
EcoFlow Grid Charge Enable/Disable Test
========================================
Tests the /provider-service/app/device/provider/digest/createOrUpdate endpoint
which controls grid-charge-to-battery via the isPTOApprove field.

Usage:
  python ecoflow_grid_charge_test.py          # read current state + enable charging
  python ecoflow_grid_charge_test.py disable  # disable grid charge
  python ecoflow_grid_charge_test.py read     # read only, no changes
"""

import json
import os
import sys
import urllib.request

GATEWAY_SN = "HR65ZA1AVH7J0027"
BASE_URL   = "https://api-a.ecoflow.com"

# ── Load credentials ──────────────────────────────────────────────────────────
def load_jwt():
    cred_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "ecoflow_credentials.txt")
    if not os.path.exists(cred_file):
        print(f"ERROR: {cred_file} not found")
        sys.exit(1)
    for line in open(cred_file).read().splitlines():
        line = line.strip()
        if line.startswith("REST_JWT="):
            jwt = line.split("=", 1)[1].strip()
            if jwt:
                return jwt
    print("ERROR: REST_JWT not found or empty in ecoflow_credentials.txt")
    sys.exit(1)


def make_headers(jwt):
    return {
        "Authorization": f"Bearer {jwt}",
        "Content-Type":  "application/json",
        "lang":          "en-us",
        "countryCode":   "US",
        "platform":      "android",
        "version":       "6.11.0.1731",
        "User-Agent":    "okhttp/4.11.0",
        "X-Appid":       "-1",
    }


def do_get(url, headers):
    req  = urllib.request.Request(url, headers=headers, method="GET")
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read())


def do_post(url, headers, body):
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data, headers=headers, method="POST")
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read())


# ── Step 1: Read current device summary ──────────────────────────────────────
def read_device_detail(jwt):
    print("\n── STEP 1: Read current device summary ──────────────────────────")
    url = f"{BASE_URL}/provider-service/app/device/provider/digest/detail?sn={GATEWAY_SN}"
    print(f"GET {url}")
    try:
        result = do_get(url, make_headers(jwt))
        print(f"Response code: {result.get('code')}  msg: {result.get('message','?')}")
        data = result.get("data") or result.get("Data")
        if data:
            # Look for isPTOApprove in the data
            setting_info = None
            if isinstance(data, dict):
                setting_info = data.get("settingInfo") or data.get("SettingInfo")
                # Sometimes it's nested differently — print top-level keys to explore
                print(f"Top-level keys in data: {list(data.keys())}")
                if setting_info:
                    print(f"settingInfo raw: {setting_info}")
                    if isinstance(setting_info, str):
                        try:
                            parsed = json.loads(setting_info)
                            print(f"settingInfo parsed: {json.dumps(parsed, indent=2)}")
                            pto = parsed.get("isPTOApprove")
                            print(f"\n>>> isPTOApprove = {repr(pto)}  "
                                  f"({'ENABLED' if pto == '1' else 'DISABLED' if pto == '0' else 'UNKNOWN'})")
                        except Exception:
                            pass
                else:
                    print(f"Full data: {json.dumps(data, indent=2)[:2000]}")
            else:
                print(f"data (non-dict): {str(data)[:500]}")
        else:
            print(f"Full response: {json.dumps(result, indent=2)[:2000]}")
        return result
    except Exception as e:
        print(f"ERROR: {e}")
        return None


# ── Step 2: Set isPTOApprove ──────────────────────────────────────────────────
def set_grid_charge(jwt, enable: bool):
    value = "1" if enable else "0"
    action = "ENABLE" if enable else "DISABLE"
    print(f"\n── STEP 2: {action} grid charge to battery (isPTOApprove={value}) ──")

    url  = f"{BASE_URL}/provider-service/app/device/provider/digest/createOrUpdate"
    body = {
        "sn":              GATEWAY_SN,
        "systemNo":        "",
        "settingInfo":     json.dumps({"isPTOApprove": value, "feedPower": "5000"}),
        "checkResultInfo": None,
        "infos":           None,
        "deviceInfo":      None,
    }
    print(f"POST {url}")
    print(f"Body: {json.dumps(body, indent=2)}")
    try:
        result = do_post(url, make_headers(jwt), body)
        code   = result.get("code")
        msg    = result.get("message", "?")
        print(f"\nResponse code: {code}  msg: {msg}")
        if code == "0":
            print(f">>> SUCCESS — grid charge {action}D")
        else:
            print(f">>> FAILED — code={code}")
            print(f"Full response: {json.dumps(result, indent=2)}")
        return result
    except Exception as e:
        print(f"ERROR: {e}")
        return None


# ── Step 3: Re-read to confirm change ────────────────────────────────────────
def confirm_change(jwt):
    print("\n── STEP 3: Re-read to confirm change ────────────────────────────")
    read_device_detail(jwt)


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "enable"

    jwt = load_jwt()
    print(f"JWT loaded (first 20 chars): {jwt[:20]}...")

    read_device_detail(jwt)

    if mode == "read":
        print("\n(read-only mode — no changes made)")
    elif mode == "disable":
        set_grid_charge(jwt, enable=False)
        confirm_change(jwt)
    else:  # default: enable
        set_grid_charge(jwt, enable=True)
        confirm_change(jwt)

    print("\n" + "="*60)
    input("Done. Press Enter to close...")
