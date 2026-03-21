"""
Configuration: credential loading, constants, MQTT topics, color palette.
"""

import json
import logging
import os

log = logging.getLogger("ecoflow")

# ─── Device identifiers ────────────────────────────────────────────────────
MQTT_HOST   = "mqtt-a.ecoflow.com"
MQTT_PORT   = 8883
GATEWAY_SN  = "HR65ZA1AVH7J0027"
INVERTER_SN = "P101ZA1A9HA70164"

# ─── Timing ─────────────────────────────────────────────────────────────────
HISTORY_SECONDS    = 900   # 15 minutes of power history
HISTORY_POINTS     = 180   # one sample every 5s
COMED_POLL_SECONDS = 60    # Poll every 60s to minimize delay (ComEd publishes every 5 min)

# ─── ComEd ──────────────────────────────────────────────────────────────────
COMED_FIXED_RATE   = 9.6   # cents/kWh (Price to Compare as of Jan 2026)
COMED_5MIN_URL     = "https://hourlypricing.comed.com/api?type=5minutefeed"
COMED_HOURAVG_URL  = "https://hourlypricing.comed.com/api?type=currenthouraverage"

# ─── Battery ──────────────────────────────────────────────────────────────
BATTERY_CAPACITY_WH = 49152   # 8 × 6144 Wh (Delta Pro Ultra X × 8 batteries)

# ─── Paths ────────────────────────────────────────────────────────────────
_PROJECT_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THRESHOLDS_FILE = os.path.join(_PROJECT_DIR, "ecoflow_thresholds.json")
STATE_FILE      = os.path.join(_PROJECT_DIR, "ecoflow_state.json")
CREDENTIALS_FILE = os.path.join(_PROJECT_DIR, "ecoflow_credentials.txt")

# ─── Credential file loader ────────────────────────────────────────────────
def _load_credentials():
    creds = {
        "MQTT_USER": "app-740f41d44de04eaf83832f8a801252e9",
        "MQTT_PASS": "c1e46f17f6994a1e8252f1e1f3135b68",
        "CLIENT_ID": "ANDROID_892461037_1971363830522871810",
        "REST_JWT":  "",
    }
    if os.path.exists(CREDENTIALS_FILE):
        for line in open(CREDENTIALS_FILE).read().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k in creds:
                    creds[k] = v
        log.info("Credentials loaded from %s", CREDENTIALS_FILE)
    else:
        with open(CREDENTIALS_FILE, "w") as f:
            f.write("# EcoFlow MQTT Credentials\n")
            f.write("# When credentials change, update these three lines and restart.\n")
            f.write(f"MQTT_USER={creds['MQTT_USER']}\n")
            f.write(f"MQTT_PASS={creds['MQTT_PASS']}\n")
            f.write(f"CLIENT_ID={creds['CLIENT_ID']}\n")
        log.info("Created credential file: %s", CREDENTIALS_FILE)
    return creds

_creds    = _load_credentials()
MQTT_USER = _creds["MQTT_USER"]
MQTT_PASS = _creds["MQTT_PASS"]
CLIENT_ID = _creds["CLIENT_ID"]
REST_JWT  = _creds["REST_JWT"]

# SESSION_ID = user ID portion of CLIENT_ID (e.g. "1971363830522871810")
_id_parts  = CLIENT_ID.split("_", 2)
SESSION_ID = _id_parts[2] if len(_id_parts) >= 3 else _id_parts[-1]

# Note: EcoFlow broker validates client ID format — must be ANDROID_{random}_{userId}.
# Don't append suffixes or the broker rejects with rc=5.

# ─── MQTT Topics ────────────────────────────────────────────────────────────
TELEMETRY_TOPICS = [
    f"/app/device/property/{GATEWAY_SN}",
    f"/app/device/property/{INVERTER_SN}",
]
COMMAND_TOPIC = f"/app/{SESSION_ID}/{GATEWAY_SN}/thing/property/set"
GET_TOPIC     = f"/app/{SESSION_ID}/{GATEWAY_SN}/thing/property/get"

# ─── Color palette (semantic colors for frontend reference) ─────────────────
COLORS = {
    "bg":            "#0d1117",
    "panel":         "#161b22",
    "panel2":        "#1c2333",
    "border":        "#30363d",
    "text":          "#e6edf3",
    "dim":           "#8b949e",
    "amber":         "#f0a500",
    "green":         "#3fb950",
    "red":           "#f85149",
    "blue":          "#58a6ff",
    "purple":        "#bc8cff",
    "gold":          "#d4a017",
    "grid_line":     "#21262d",
    "flow_grid":     "#f0a500",
    "flow_batt_ch":  "#3fb950",
    "flow_batt_dis": "#f85149",
    "flow_load":     "#58a6ff",
}
