"""
EcoFlow Home Energy Dashboard  v2.0
=====================================
Full-featured desktop dashboard for EcoFlow Smart Gateway + Delta Pro Ultra,
with ComEd BESH real-time pricing and automated charge/discharge control.

Features:
  • Animated real-time power flow diagram
  • ComEd 5-minute + hourly pricing with trend indicator and sparkline
  • Automated charge/discharge decisions based on price thresholds
  • Adjustable automation thresholds via UI (no code editing)
  • Manual mode + charge controls (DRY RUN by default)
  • 15-minute scrolling history graph
  • Credential file — update ecoflow_credentials.txt when creds change

Dependencies:
  pip install paho-mqtt

Run:
  python ecoflow_dashboard.py
"""

import datetime
import json
import logging
import math
import os as _os
import random
import struct
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
import tkinter as tk
from tkinter import font as tkfont
import paho.mqtt.client as mqtt


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
MQTT_HOST   = "mqtt-a.ecoflow.com"
MQTT_PORT   = 8883
GATEWAY_SN  = "HR65ZA1AVH7J0027"
INVERTER_SN = "P101ZA1A9HA70164"


HISTORY_SECONDS    = 900   # 15 minutes of power history
HISTORY_POINTS     = 180   # one sample every 5s
COMED_POLL_SECONDS = 300   # ComEd publishes a new 5-min price every 5 minutes

# ComEd fixed-rate comparison (Price to Compare as of Jan 2026)
COMED_FIXED_RATE = 9.6    # cents/kWh

# ── Credential file loader ────────────────────────────────────────────────────
def _load_credentials():
    _dir      = _os.path.dirname(_os.path.abspath(__file__))
    cred_file = _os.path.join(_dir, "ecoflow_credentials.txt")
    creds = {
        "MQTT_USER": "app-740f41d44de04eaf83832f8a801252e9",
        "MQTT_PASS": "c1e46f17f6994a1e8252f1e1f3135b68",
        "CLIENT_ID": "ANDROID_574080605_1971363830522871810",
        "REST_JWT":  "",
    }
    if _os.path.exists(cred_file):
        for line in open(cred_file).read().splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k in creds:
                    creds[k] = v
        print(f"  Credentials loaded from {cred_file}")
    else:
        with open(cred_file, "w") as f:
            f.write("# EcoFlow MQTT Credentials\n")
            f.write("# When credentials change, update these three lines and restart.\n")
            f.write("# Get new values: EcoFlow app -> Me -> IoT Developer -> MQTT Credentials\n")
            f.write(f"MQTT_USER={creds['MQTT_USER']}\n")
            f.write(f"MQTT_PASS={creds['MQTT_PASS']}\n")
            f.write(f"CLIENT_ID={creds['CLIENT_ID']}\n")
        print(f"  Created credential file: {cred_file}")
    return creds

_creds    = _load_credentials()
MQTT_USER = _creds["MQTT_USER"]
MQTT_PASS = _creds["MQTT_PASS"]
CLIENT_ID = _creds["CLIENT_ID"]
REST_JWT  = _creds["REST_JWT"]
# SESSION_ID = 3rd segment of CLIENT_ID (e.g. "1971363830522871810")
# This is the routing ID in topics: /app/{SESSION_ID}/{device}/set
_id_parts  = CLIENT_ID.split("_", 2)
SESSION_ID = _id_parts[2] if len(_id_parts) >= 3 else _id_parts[-1]

TELEMETRY_TOPICS = [
    f"/app/device/property/{GATEWAY_SN}",
    f"/app/device/property/{INVERTER_SN}",
]
COMMAND_TOPIC = f"/app/{SESSION_ID}/{GATEWAY_SN}/thing/property/set"
GET_TOPIC     = f"/app/{SESSION_ID}/{GATEWAY_SN}/thing/property/get"


# ─────────────────────────────────────────────────────────────────────────────
# COLOUR PALETTE
# ─────────────────────────────────────────────────────────────────────────────
C = {
    "bg":            "#0d1117",
    "panel":         "#161b22",
    "panel2":        "#1c2333",
    "border":        "#30363d",
    "text":          "#e6edf3",
    "dim":           "#8b949e",
    "amber":         "#f0a500",
    "amber_dim":     "#7a5200",
    "green":         "#3fb950",
    "green_dim":     "#1a4a22",
    "red":           "#f85149",
    "red_dim":       "#4a1a1a",
    "blue":          "#58a6ff",
    "blue_dim":      "#1a3a6a",
    "purple":        "#bc8cff",
    "gold":          "#d4a017",
    "grid_line":     "#21262d",
    "flow_grid":     "#f0a500",
    "flow_batt_ch":  "#3fb950",
    "flow_batt_dis": "#f85149",
    "flow_load":     "#58a6ff",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dashboard")


# ─────────────────────────────────────────────────────────────────────────────
# PROTOBUF DECODER
# ─────────────────────────────────────────────────────────────────────────────
class ProtoDecoder:
    @staticmethod
    def decode_varint(data: bytes, pos: int):
        result, shift = 0, 0
        while pos < len(data):
            b = data[pos]; pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80): break
            shift += 7
        return result, pos

    @staticmethod
    def decode_message(data: bytes) -> dict:
        fields = {}
        pos = 0
        while pos < len(data):
            if pos >= len(data): break
            tag, pos = ProtoDecoder.decode_varint(data, pos)
            field_num = tag >> 3
            wire_type = tag & 0x07
            if field_num == 0: break
            if wire_type == 0:
                val, pos = ProtoDecoder.decode_varint(data, pos)
                fields[field_num] = val
            elif wire_type == 2:
                length, pos = ProtoDecoder.decode_varint(data, pos)
                raw = data[pos: pos + length]; pos += length
                nested = ProtoDecoder.decode_message(raw)
                fields[field_num] = {"_bytes": raw, "_nested": nested}
            elif wire_type == 5:
                val = struct.unpack_from("<f", data, pos)[0]; pos += 4
                fields[field_num] = val
            else:
                break
        return fields

    @staticmethod
    def get_float(fields: dict, *path: int) -> Optional[float]:
        node = fields
        for key in path[:-1]:
            entry = node.get(key)
            if not isinstance(entry, dict): return None
            node = entry.get("_nested", {})
        val = node.get(path[-1])
        if val is None: return None
        try: return float(val)
        except: return None

    @staticmethod
    def get_int(fields: dict, *path: int) -> Optional[int]:
        v = ProtoDecoder.get_float(fields, *path)
        return int(v) if v is not None else None


# ─────────────────────────────────────────────────────────────────────────────
# PROTOBUF COMMAND ENCODER
# ─────────────────────────────────────────────────────────────────────────────
# ── Protobuf encoding primitives (confirmed working, ported from test script) ──

def _encode_varint(value):
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)

def _encode_field_varint(field_number, value, force=False):
    if value == 0 and not force:
        return b""
    tag = (field_number << 3) | 0
    return _encode_varint(tag) + _encode_varint(value)

def _encode_field_bool(field_number, value):
    if not value:
        return b""
    tag = (field_number << 3) | 0
    return _encode_varint(tag) + _encode_varint(1)

def _encode_field_bytes(field_number, data):
    tag = (field_number << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(data)) + data

def _encode_field_string(field_number, s):
    return _encode_field_bytes(field_number, s.encode("utf-8"))

def _encode_field_message(field_number, message_bytes):
    return _encode_field_bytes(field_number, message_bytes)

# ── SHP3 command builders (DevAplComm.ConfigWrite → Common.Header → Send_Header_Msg) ──

def build_mode_command(self_powered=False, scheduled=False, tou=False):
    """CfgPanelEnergyStrategyOperateMode on ConfigWrite field 544."""
    mode_msg = b""
    mode_msg += _encode_field_bool(1, self_powered)
    mode_msg += _encode_field_bool(2, scheduled)
    mode_msg += _encode_field_bool(3, tou)
    return _encode_field_message(544, mode_msg)

def build_charge_command(enable, channel=1, use_normal_chg=False):
    """BackupCtrl on ConfigWrite field 535+channel. 1=ON, 2=OFF."""
    charge_val = 1 if enable else 2
    backup_ctrl = b""
    backup_ctrl += _encode_field_varint(1, 1)          # ctrlEn = 1
    if use_normal_chg:
        backup_ctrl += _encode_field_varint(3, charge_val)  # ctrlNormalChg
    else:
        backup_ctrl += _encode_field_varint(2, charge_val)  # ctrlForceChg
    field_num = 534 + channel
    return _encode_field_message(field_num, backup_ctrl)

def build_charge_power_command(watts, max_soc=None):
    """ConfigWrite field 542 (watts) + optional field 33 (SOC %)."""
    msg = b""
    msg += _encode_field_varint(542, watts)
    if max_soc is not None:
        msg += _encode_field_varint(33, max_soc)
    return msg

def build_header(pdata, seq):
    """Common.Header: dest=11, src=32, cmdSet=254, cmdId=17."""
    msg = b""
    msg += _encode_field_bytes(1, pdata)
    msg += _encode_field_varint(2, 32)        # src
    msg += _encode_field_varint(3, 11)        # dest (SHP3)
    msg += _encode_field_varint(4, 1)         # dSrc
    msg += _encode_field_varint(5, 1)         # dDest
    msg += _encode_field_varint(8, 254)       # cmdFunc
    msg += _encode_field_varint(9, 17)        # cmdId
    msg += _encode_field_varint(10, len(pdata))
    msg += _encode_field_varint(11, 1)        # needAck
    msg += _encode_field_varint(14, seq)
    msg += _encode_field_varint(15, 1)        # productId
    msg += _encode_field_varint(16, 19)       # version
    msg += _encode_field_varint(17, 1)        # payloadVer
    msg += _encode_field_string(23, "Android")
    return msg

def build_send_header_msg(header_bytes):
    """Outer wrapper: Send_Header_Msg field 1."""
    return _encode_field_message(1, header_bytes)

def _build_and_wrap(config_write_bytes):
    """Full pipeline: ConfigWrite → Header → Send_Header_Msg. Returns ready-to-publish bytes."""
    seq = random.randint(100000, 999999)
    header = build_header(config_write_bytes, seq)
    return build_send_header_msg(header)


# ─────────────────────────────────────────────────────────────────────────────
# APPLICATION STATE
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class PowerState:
    grid_w:      Optional[float] = None
    load_w:      Optional[float] = None
    battery_w:   Optional[float] = None   # + charging, - discharging
    volt_a:      Optional[float] = None
    volt_b:      Optional[float] = None
    op_mode:     Optional[int]   = None   # 1=backup, 2=self-powered
    soc_pct:     Optional[float] = None
    last_update: float           = 0.0

    @property
    def battery_charging(self):    return (self.battery_w or 0) > 50
    @property
    def battery_discharging(self): return (self.battery_w or 0) < -50
    @property
    def mode_label(self):          return {1: "BACKUP", 2: "SELF-POWERED"}.get(self.op_mode, "—")
    @property
    def stale(self):               return (time.time() - self.last_update) > 10


@dataclass
class PriceState:
    price_5min:      Optional[float] = None   # cents/kWh, most recent 5-min interval
    price_hour:      Optional[float] = None   # cents/kWh, current hour average (ComEd API)
    running_hour_avg: Optional[float] = None  # cents/kWh, our own running avg this hour
    effective_price: Optional[float] = None   # cents/kWh, conservative of the two
    hour_prices:     list            = field(default_factory=list)  # 5-min prices in current hour
    _current_hour:   int             = -1     # hour number for resetting hour_prices
    trend:           str             = "flat" # "rising" | "falling" | "flat"
    trend_slope:     float           = 0.0
    tier:            str             = "—"
    tier_color:      str             = "#8b949e"
    history_5min:    list            = field(default_factory=list)  # [(ts, price), ...] newest first
    last_update:     float           = 0.0
    error:           str             = ""

    @property
    def stale(self): return self.last_update > 0 and (time.time() - self.last_update) > 180


def parse_payload(payload: bytes, state: PowerState) -> bool:
    try:
        outer   = ProtoDecoder.decode_message(payload)
        level1  = outer.get(1)
        if not isinstance(level1, dict): return False
        nested1 = level1.get("_nested", {})
        inner   = nested1.get(1)
        data    = inner.get("_nested", {}) if isinstance(inner, dict) else nested1
        if not data: return False

        gf = lambda *p: ProtoDecoder.get_float(data, *p)
        gi = lambda *p: ProtoDecoder.get_int(data, *p)
        updated = False

        def _s(attr, val):
            nonlocal updated
            if val is not None:
                setattr(state, attr, val)
                updated = True

        _s("battery_w", gf(518))
        _s("soc_pct",   gf(262))     # CMS_BATT_SOC (float, from HR65 gateway)
        _s("volt_a",    gf(1063))
        _s("volt_b",    gf(1064))

        # f1544 = reliable home load in ALL modes (f515 drops to 0 during discharge)
        load = gi(1544)
        if load is not None and load > 0:
            _s("load_w", float(load))

        # Grid = Home + Battery (signed). No direct grid field exists in telemetry.
        batt = gf(518)
        if load is not None and batt is not None:
            _s("grid_w", max(0.0, float(load) + batt))
        elif load is not None:
            _s("grid_w", float(load))

        # Mode: f1009 sub[4]=2 → Self-Powered; sub[4] absent → Backup
        m = data.get(1009)
        if isinstance(m, dict):
            sub4 = m.get("_nested", {}).get(4)
            if isinstance(sub4, int) and sub4 == 2:
                _s("op_mode", 2)
            elif sub4 is None:
                _s("op_mode", 1)

        if updated:
            state.last_update = time.time()
        return updated
    except Exception as e:
        log.debug("parse error: %s", e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# HISTORY BUFFER
# ─────────────────────────────────────────────────────────────────────────────
class HistoryBuffer:
    def __init__(self, maxlen=HISTORY_POINTS):
        self.times   = deque(maxlen=maxlen)
        self.grid    = deque(maxlen=maxlen)
        self.load    = deque(maxlen=maxlen)
        self.battery = deque(maxlen=maxlen)
        self._last   = 0.0

    def maybe_add(self, state: PowerState):
        now = time.time()
        if now - self._last < 5.0: return
        self._last = now
        self.times.append(now)
        self.grid.append(state.grid_w or 0)
        self.load.append(state.load_w or 0)
        self.battery.append(state.battery_w or 0)


# ─────────────────────────────────────────────────────────────────────────────
# COMED PRICE ENGINE
# ─────────────────────────────────────────────────────────────────────────────
COMED_5MIN_URL    = "https://hourlypricing.comed.com/api?type=5minutefeed"
COMED_HOURAVG_URL = "https://hourlypricing.comed.com/api?type=currenthouraverage"


def _classify_price(cents: float) -> tuple:
    """Return (tier_label, hex_color) for a price in cents/kWh."""
    if cents < 0:                    return "NEGATIVE",  "#00ff88"
    elif cents < 3:                  return "VERY LOW",  "#3fb950"
    elif cents < 6:                  return "LOW",       "#58a6ff"
    elif cents < COMED_FIXED_RATE:   return "MODERATE",  "#f0a500"
    elif cents < 14:                 return "HIGH",      "#f85149"
    else:                            return "SPIKE",     "#ff4040"


def _price_trend(entries: list, n=6) -> tuple:
    """
    entries: [(timestamp, price_cents), ...] newest first.
    Returns (direction_str, slope_float) — positive slope = rising over time.
    """
    if len(entries) < 2: return "flat", 0.0
    # Reverse slice to get chronological order for regression
    chrono = [p for _, p in reversed(entries[:n])]
    n_pts  = len(chrono)
    xm     = (n_pts - 1) / 2
    ym     = sum(chrono) / n_pts
    num    = sum((i - xm) * (chrono[i] - ym) for i in range(n_pts))
    den    = sum((i - xm) ** 2 for i in range(n_pts))
    slope  = num / den if den else 0.0
    if slope > 0.3:    return "rising",  round(slope, 3)
    elif slope < -0.3: return "falling", round(slope, 3)
    else:              return "flat",    round(slope, 3)


class ComedPoller:
    """Polls ComEd APIs in a background thread every COMED_POLL_SECONDS."""

    def __init__(self, price_state: PriceState, on_update):
        self.ps         = price_state
        self.on_update  = on_update
        self._stop      = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        self._poll()
        while not self._stop.wait(COMED_POLL_SECONDS):
            self._poll()

    def _poll(self):
        try:
            with urllib.request.urlopen(COMED_5MIN_URL, timeout=10) as r:
                raw_5min = json.loads(r.read())
            with urllib.request.urlopen(COMED_HOURAVG_URL, timeout=10) as r:
                raw_hour = json.loads(r.read())

            entries = sorted(
                [(int(x["millisUTC"]) / 1000.0, float(x["price"])) for x in raw_5min],
                key=lambda e: e[0], reverse=True
            )
            if not entries: return

            current   = entries[0][1]
            hour_avg  = float(raw_hour[0]["price"]) if raw_hour else None
            trend, sl = _price_trend(entries)
            tier, col = _classify_price(current)

            self.ps.price_5min   = current
            self.ps.price_hour   = hour_avg
            self.ps.trend        = trend
            self.ps.trend_slope  = sl
            self.ps.tier         = tier
            self.ps.tier_color   = col
            self.ps.history_5min = entries[:12]
            self.ps.last_update  = time.time()
            self.ps.error        = ""

            # Running average: last 4 five-minute prices (~20 min rolling window)
            # Display only — not used for automation decisions.
            last4 = [p for _, p in entries[:4]]
            if last4:
                self.ps.running_hour_avg = sum(last4) / len(last4)
            else:
                self.ps.running_hour_avg = hour_avg

            # Effective price = ComEd hourly average (what BESH actually bills on).
            # Running hour avg and 5-min trend are display-only indicators.
            if hour_avg is not None:
                self.ps.effective_price = hour_avg
            else:
                # Fallback: no hourly avg yet (rare) — use latest 5-min price
                self.ps.effective_price = current

            log.info("ComEd: 5min=%.1f¢  hour=%s¢  running=%s¢  eff=%s¢  trend=%s(%.2f)  tier=%s",
                     current,
                     f"{hour_avg:.1f}" if hour_avg else "?",
                     f"{self.ps.running_hour_avg:.1f}" if self.ps.running_hour_avg else "?",
                     f"{self.ps.effective_price:.1f}" if self.ps.effective_price else "?",
                     trend, sl, tier)
            self.on_update()

        except Exception as e:
            self.ps.error = str(e)
            log.warning("ComEd poll failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# AUTOMATION THRESHOLDS & CONTROLLER
# ─────────────────────────────────────────────────────────────────────────────
THRESHOLDS_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "ecoflow_thresholds.json")

@dataclass
class AutoThresholds:
    # Discharge: switch to Self-Powered (battery powers home) above this price
    discharge_above:    float = 8.0    # ¢/kWh

    # SOC-tiered charging with FLOOR model:
    #   Emergency (0% → low_floor):           charge below emergency_charge_below
    #   Low       (low_floor → mid_floor):    charge below low_charge_below
    #   Mid       (mid_floor → high_floor):   charge below mid_charge_below
    #   High      (high_floor → max_soc):     charge below high_charge_below
    #   Above max_soc:                        stop charging

    low_floor:          float = 20.0   # % — below this is emergency
    low_charge_below:   float = 2.0    # ¢ — generous (battery is low)
    low_rate:           int   = 6000   # W — charge fast

    mid_floor:          float = 60.0   # % — below this is low band
    mid_charge_below:   float = 1.5    # ¢ — moderate
    mid_rate:           int   = 3000   # W

    high_floor:         float = 85.0   # % — below this is mid band
    high_charge_below:  float = -1.0   # ¢ — only very cheap/negative
    high_rate:          int   = 1500   # W

    max_soc:            float = 95.0   # % — stop charging above this

    rate_emergency:     int   = 6000   # W — fast charge in emergency
    emergency_charge_below: float = 6.0   # ¢ — even emergency has a price cap

    def save(self):
        """Persist current thresholds to JSON file."""
        from dataclasses import asdict
        try:
            with open(THRESHOLDS_FILE, "w") as f:
                json.dump(asdict(self), f, indent=2)
        except Exception as e:
            log.warning("Failed to save thresholds: %s", e)

    @classmethod
    def load(cls):
        """Load thresholds from JSON file, falling back to defaults."""
        t = cls()
        if _os.path.exists(THRESHOLDS_FILE):
            try:
                with open(THRESHOLDS_FILE) as f:
                    saved = json.load(f)
                for k, v in saved.items():
                    if hasattr(t, k):
                        setattr(t, k, type(getattr(t, k))(v))
                log.info("Loaded thresholds from %s", THRESHOLDS_FILE)
            except Exception as e:
                log.warning("Failed to load thresholds: %s", e)
        return t


class AutoController:
    MIN_HOLD = 30   # seconds between commands (reduced from 120 for responsiveness)

    def __init__(self):
        self.enabled        = False
        self.last_mode      = None
        self.last_rate      = None
        self.last_cmd_ts    = 0.0
        self.last_decision  = "—"

    def decide(self, ps: PriceState, pw: PowerState, t: AutoThresholds):
        """
        Returns (target_mode, target_rate_w, reason). None = no change.

        SOC floor model — bands read bottom-up:
          Emergency (0% → low_floor):         charge at ANY price
          Low       (low_floor → mid_floor):  charge below low_charge_below
          Mid       (mid_floor → high_floor): charge below mid_charge_below
          High      (high_floor → max_soc):   charge below high_charge_below
          Above max_soc:                      stop charging
        """
        soc = pw.soc_pct

        # Effective price = ComEd hourly average (BESH billing rate).
        # Trend / running avg are display-only — not used for decisions.
        if ps.effective_price is not None:
            ep  = ps.effective_price
            src = "hr"
        elif ps.price_hour is not None:
            ep  = ps.price_hour
            src = "hr"
        elif ps.price_5min is not None:
            ep  = ps.price_5min
            src = "5m"
        else:
            return None, None, "waiting for price data"

        # Battery full — stop charging, just decide on mode
        if soc is not None and soc >= t.max_soc:
            if ep >= t.discharge_above:
                return 2, 0, f"DISCHARGE: full + {ep:.1f}c [{src}] >= {t.discharge_above:.1f}c"
            return 1, 0, f"HOLD: battery full ({soc:.0f}% >= {t.max_soc:.0f}%)"

        # High price → self-powered (discharge)
        if ep >= t.discharge_above:
            if soc is None or soc > t.low_floor:
                return 2, 0, f"DISCHARGE: {ep:.1f}c [{src}] >= {t.discharge_above:.1f}c"
            return 1, 0, f"HOLD: price high but SOC {soc:.0f}% too low"

        # SOC-tiered charging decision (floor model)
        if soc is None:
            # No SOC data — use mid band as safe default
            if ep < t.mid_charge_below:
                return 1, t.mid_rate, f"CHARGE: {ep:.1f}c [{src}] (no SOC, mid default)"
            return 1, 0, f"HOLD: {ep:.1f}c [{src}] (no SOC)"

        if soc < t.low_floor:
            # EMERGENCY — below low floor, charge if price below emergency cap
            if ep < t.emergency_charge_below:
                return 1, t.rate_emergency, f"EMERGENCY: SOC {soc:.0f}% < {t.low_floor:.0f}%  {ep:.1f}c < {t.emergency_charge_below:.1f}c [{src}]"
            return 1, 0, f"HOLD EMERGENCY: {ep:.1f}c [{src}] >= {t.emergency_charge_below:.1f}c  SOC {soc:.0f}%"

        if soc < t.mid_floor:
            # LOW band — charge at generous prices
            if ep < t.low_charge_below:
                return 1, t.low_rate, f"CHARGE LOW: {ep:.1f}c [{src}] < {t.low_charge_below:.1f}c  SOC {soc:.0f}%"
            return 1, 0, f"HOLD LOW: {ep:.1f}c [{src}] >= {t.low_charge_below:.1f}c  SOC {soc:.0f}%"

        if soc < t.high_floor:
            # MID band — charge at moderate prices
            if ep < t.mid_charge_below:
                return 1, t.mid_rate, f"CHARGE MID: {ep:.1f}c [{src}] < {t.mid_charge_below:.1f}c  SOC {soc:.0f}%"
            return 1, 0, f"HOLD MID: {ep:.1f}c [{src}] >= {t.mid_charge_below:.1f}c  SOC {soc:.0f}%"

        # HIGH band (high_floor → max_soc) — only charge when very cheap
        if ep < t.high_charge_below:
            return 1, t.high_rate, f"CHARGE HIGH: {ep:.1f}c [{src}] < {t.high_charge_below:.1f}c  SOC {soc:.0f}%"
        return 1, 0, f"HOLD HIGH: {ep:.1f}c [{src}] >= {t.high_charge_below:.1f}c  SOC {soc:.0f}%"

    def should_send(self, target_mode, target_rate):
        if not self.enabled:
            return False, "automation off"
        mode_changed = target_mode is not None and target_mode != self.last_mode
        rate_changed = target_rate is not None and target_rate != self.last_rate
        if not mode_changed and not rate_changed:
            return False, "no change"
        if self.last_cmd_ts > 0:
            elapsed = time.time() - self.last_cmd_ts
            if elapsed < self.MIN_HOLD:
                return False, f"hold {self.MIN_HOLD - elapsed:.0f}s"
        return True, "ok"

    def record(self, target_mode, target_rate, reason):
        if target_mode is not None: self.last_mode = target_mode
        if target_rate is not None: self.last_rate = target_rate
        self.last_cmd_ts   = time.time()
        self.last_decision = reason


# ─────────────────────────────────────────────────────────────────────────────
# MQTT CLIENT
# ─────────────────────────────────────────────────────────────────────────────
class MQTTHandler:
    def __init__(self, state: PowerState, history: HistoryBuffer, on_update):
        self.state       = state
        self.history     = history
        self.on_update   = on_update
        self.connected   = False
        self.last_msg_ts = 0.0
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION1,
            client_id=CLIENT_ID,
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
        self._client.username_pw_set(MQTT_USER, MQTT_PASS)
        self._client.tls_set()
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        self.connected = (rc == 0)
        if rc == 0:
            log.info("MQTT connected OK")
            for t in TELEMETRY_TOPICS:
                client.subscribe(t, qos=1)
            # Trigger telemetry — gateway doesn't stream on its own
            self._request_quotas(client)
        else:
            log.error("MQTT connect failed rc=%d", rc)

    def _request_quotas(self, client):
        """Send latestQuotas GET to trigger telemetry from both devices."""
        for sn in (GATEWAY_SN, INVERTER_SN):
            get_topic = f"/app/{SESSION_ID}/{sn}/thing/property/get"
            msg = json.dumps({
                "from": "Android",
                "id": str(int(time.time() * 1000)),
                "moduleSn": sn,
                "moduleType": 0,
                "operateType": "latestQuotas",
                "params": {},
                "version": "1.0",
                "lang": "en-us",
            })
            client.publish(get_topic, msg.encode(), qos=1)
            log.info("Sent latestQuotas GET to %s", sn)

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            log.warning("MQTT disconnected rc=%d", rc)

    def _on_message(self, client, userdata, msg):
        self.last_msg_ts = time.time()
        if parse_payload(msg.payload, self.state):
            self.history.maybe_add(self.state)
            self.on_update()

    def start(self):
        self._client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=120)
        self._client.loop_start()

    def publish_command(self, payload: bytes, commands_live: bool = False):
        """Send a protobuf-encoded command. payload must be ready-to-publish bytes."""
        if commands_live:
            rc = self._client.publish(COMMAND_TOPIC, payload, qos=1)
            log.info("CMD LIVE rc=%s  %d bytes: %s", rc.rc, len(payload), payload.hex())
        else:
            log.info("CMD DRY  %d bytes: %s", len(payload), payload.hex())


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────
class Dashboard:
    REFRESH_MS = 250

    def __init__(self):
        self.state      = PowerState()
        self.price      = PriceState()
        self.history    = HistoryBuffer()
        self.thresholds = AutoThresholds.load()
        self.auto       = AutoController()
        self.mqtt       = MQTTHandler(self.state, self.history, self._on_mqtt_update)
        self.comed      = ComedPoller(self.price, self._on_price_update)
        self._dirty          = False
        self._lock           = threading.Lock()
        self._thresh_vars    = {}     # attr -> (StringVar, update_fn)
        self._commands_live  = False  # toggled via UI button

        self._build_window()
        self.mqtt.start()
        self.comed.start()
        self._tick()

    # ─────────────────────────────────────────────────────────────────────────
    # WINDOW CONSTRUCTION
    # ─────────────────────────────────────────────────────────────────────────
    def _build_window(self):
        self.root = tk.Tk()
        self.root.title("EcoFlow Energy Dashboard  v2.0")
        self.root.configure(bg=C["bg"])
        self.root.geometry("1500x900")
        self.root.minsize(1200, 720)

        try:
            self.fn_mono  = tkfont.Font(family="Consolas", size=11)
            self.fn_sm    = tkfont.Font(family="Consolas", size=9)
            self.fn_big   = tkfont.Font(family="Consolas", size=26, weight="bold")
            self.fn_med   = tkfont.Font(family="Consolas", size=13, weight="bold")
            self.fn_lbl   = tkfont.Font(family="Consolas", size=9)
            self.fn_hdr   = tkfont.Font(family="Consolas", size=10, weight="bold")
        except Exception:
            self.fn_mono  = tkfont.Font(family="Courier", size=11)
            self.fn_sm    = tkfont.Font(family="Courier", size=9)
            self.fn_big   = tkfont.Font(family="Courier", size=26, weight="bold")
            self.fn_med   = tkfont.Font(family="Courier", size=13, weight="bold")
            self.fn_lbl   = tkfont.Font(family="Courier", size=9)
            self.fn_hdr   = tkfont.Font(family="Courier", size=10, weight="bold")

        self._build_topbar()

        body = tk.Frame(self.root, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        self.left = tk.Frame(body, bg=C["bg"])
        self.left.pack(side="left", fill="both", expand=True)

        self.right = tk.Frame(body, bg=C["panel"], width=304)
        self.right.pack(side="right", fill="y", padx=(8, 0))
        self.right.pack_propagate(False)

        self._build_flow_canvas()
        self._build_history_canvas()
        self._build_controls()

    def _build_topbar(self):
        bar = tk.Frame(self.root, bg=C["panel"], height=44)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        tk.Label(bar, text="⚡ ECOFLOW ENERGY", bg=C["panel"],
                 fg=C["amber"], font=self.fn_hdr).pack(side="left", padx=16, pady=8)

        self.btn_cmd_toggle = tk.Button(
            bar, text="COMMANDS: DRY RUN",
            bg=C["amber_dim"], fg=C["amber"],
            font=self.fn_lbl, relief="flat", bd=0,
            cursor="hand2", padx=10, pady=4,
            command=self._toggle_commands
        )
        self.btn_cmd_toggle.pack(side="left", padx=12, pady=6)

        self.lbl_time     = tk.Label(bar, text="--:--:--", bg=C["panel"],
                                      fg=C["dim"], font=self.fn_sm)
        self.lbl_time.pack(side="right", padx=16)

        self.lbl_status   = tk.Label(bar, text="● CONNECTING", bg=C["panel"],
                                      fg=C["amber_dim"], font=self.fn_hdr)
        self.lbl_status.pack(side="right", padx=12)

        self.lbl_mode_top = tk.Label(bar, text="MODE: —", bg=C["panel"],
                                      fg=C["dim"], font=self.fn_hdr)
        self.lbl_mode_top.pack(side="right", padx=20)

        self.lbl_price_top = tk.Label(bar, text="PRICE: —", bg=C["panel"],
                                       fg=C["dim"], font=self.fn_hdr)
        self.lbl_price_top.pack(side="right", padx=20)

    def _build_flow_canvas(self):
        f = tk.Frame(self.left, bg=C["panel"])
        f.pack(fill="both", expand=True, pady=(0, 6))
        tk.Label(f, text="POWER FLOW", bg=C["panel"], fg=C["dim"],
                 font=self.fn_lbl).pack(anchor="nw", padx=10, pady=(6, 0))
        self.flow_canvas = tk.Canvas(f, bg=C["panel"], highlightthickness=0)
        self.flow_canvas.pack(fill="both", expand=True, padx=6, pady=(2, 6))

    def _build_history_canvas(self):
        f = tk.Frame(self.left, bg=C["panel"])
        f.pack(fill="both", expand=True)
        hdr = tk.Frame(f, bg=C["panel"])
        hdr.pack(fill="x", padx=10, pady=(6, 0))
        tk.Label(hdr, text="15-MINUTE HISTORY", bg=C["panel"], fg=C["dim"],
                 font=self.fn_lbl).pack(side="left")
        for lbl, clr in [("GRID", C["flow_grid"]), ("LOAD", C["flow_load"]),
                          ("BATTERY", C["green"])]:
            tk.Label(hdr, text=f"── {lbl}", bg=C["panel"],
                     fg=clr, font=self.fn_lbl).pack(side="right", padx=8)
        self.hist_canvas = tk.Canvas(f, bg=C["panel"], highlightthickness=0)
        self.hist_canvas.pack(fill="both", expand=True, padx=6, pady=(2, 6))

    # ─────────────────────────────────────────────────────────────────────────
    # CONTROLS PANEL
    # ─────────────────────────────────────────────────────────────────────────
    def _build_controls(self):
        p = self.right

        def sep(title):
            tk.Frame(p, bg=C["border"], height=1).pack(fill="x", padx=8, pady=(10, 3))
            tk.Label(p, text=title, bg=C["panel"], fg=C["dim"],
                     font=self.fn_lbl).pack(anchor="w", padx=12)

        tk.Label(p, text="CONTROLS", bg=C["panel"], fg=C["amber"],
                 font=self.fn_hdr).pack(pady=(12, 0), padx=12, anchor="w")

        # ── ComEd Price ──────────────────────────────────────────────────────
        sep("COMED PRICE  (¢/kWh)")

        price_row = tk.Frame(p, bg=C["panel"])
        price_row.pack(fill="x", padx=12, pady=(2, 0))
        self.lbl_price_big = tk.Label(price_row, text="—", bg=C["panel"],
                                       fg=C["dim"], font=self.fn_med)
        self.lbl_price_big.pack(side="left")
        self.lbl_trend = tk.Label(price_row, text="", bg=C["panel"],
                                   fg=C["dim"], font=self.fn_sm)
        self.lbl_trend.pack(side="left", padx=8)

        self.lbl_tier     = tk.Label(p, text="—", bg=C["panel"],
                                      fg=C["dim"], font=self.fn_lbl)
        self.lbl_tier.pack(anchor="w", padx=12)

        self.lbl_hour_avg = tk.Label(p, text="Hour avg: —", bg=C["panel"],
                                      fg=C["dim"], font=self.fn_lbl)
        self.lbl_hour_avg.pack(anchor="w", padx=12)

        self.price_canvas = tk.Canvas(p, bg=C["panel2"], highlightthickness=0, height=56)
        self.price_canvas.pack(fill="x", padx=12, pady=(4, 0))

        # ── Automation ───────────────────────────────────────────────────────
        sep("AUTOMATION")

        self.btn_auto = tk.Button(
            p, text="AUTO: OFF", font=self.fn_hdr,
            bg=C["panel2"], fg=C["dim"], relief="flat", bd=0,
            cursor="hand2", padx=8, pady=5,
            command=self._toggle_automation
        )
        self.btn_auto.pack(fill="x", padx=10, pady=(2, 0))

        self.lbl_auto_status = tk.Label(
            p, text="Enable to auto-control charging / mode",
            bg=C["panel"], fg=C["dim"], font=self.fn_lbl,
            wraplength=276, justify="left"
        )
        self.lbl_auto_status.pack(anchor="w", padx=12, pady=(2, 4))

        # Threshold adjusters — price thresholds
        thresh_f = tk.Frame(p, bg=C["panel"])
        thresh_f.pack(fill="x", padx=10)
        self._thresh_row(thresh_f, "Discharge >=",  "discharge_above",  1.0, 30.0, "c")

        # SOC-tiered charge thresholds (floor model)
        # Emergency: 0% → low_floor (charge below emergency price cap)
        # Low: low_floor → mid_floor | Mid: mid_floor → high_floor | High: high_floor → max_soc
        tk.Label(thresh_f, text="── Charge by SOC ──", bg=C["panel"],
                 fg=C["border"], font=self.fn_lbl).pack(anchor="w", pady=(4, 0))
        self._thresh_row(thresh_f, "Emergency <",   "low_floor",        5,   50, "%", 5)
        self._thresh_row(thresh_f, "  price <",     "emergency_charge_below", -5.0, 30.0, "c")
        self._thresh_row(thresh_f, "  rate",        "rate_emergency",  600, 12000, "W", 600)
        self._thresh_row(thresh_f, "Low  →",        "mid_floor",       20,   80, "%", 5)
        self._thresh_row(thresh_f, "  price <",     "low_charge_below", -5.0, 15.0, "c")
        self._thresh_row(thresh_f, "  rate",        "low_rate",        600, 12000, "W", 600)
        self._thresh_row(thresh_f, "Mid  →",        "high_floor",      40,   95, "%", 5)
        self._thresh_row(thresh_f, "  price <",     "mid_charge_below", -5.0, 10.0, "c")
        self._thresh_row(thresh_f, "  rate",        "mid_rate",        600, 12000, "W", 600)
        self._thresh_row(thresh_f, "High →",        "max_soc",         70,  100, "%", 5)
        self._thresh_row(thresh_f, "  price <",     "high_charge_below",-5.0,  5.0, "c")
        self._thresh_row(thresh_f, "  rate",        "high_rate",       600, 12000, "W", 600)

        # ── Operating Mode ───────────────────────────────────────────────────
        sep("OPERATING MODE")

        self.lbl_mode = tk.Label(p, text="—", bg=C["panel"],
                                  fg=C["text"], font=self.fn_med)
        self.lbl_mode.pack(pady=(2, 4))

        btn_row = tk.Frame(p, bg=C["panel"])
        btn_row.pack(fill="x", padx=10)
        self.btn_backup = tk.Button(
            btn_row, text="BACKUP", font=self.fn_hdr,
            bg=C["panel2"], fg=C["text"], relief="flat", bd=0,
            cursor="hand2", padx=8, pady=5,
            command=lambda: self._cmd_mode("backup")
        )
        self.btn_backup.pack(side="left", expand=True, fill="x", padx=(0, 3))
        self.btn_selfpow = tk.Button(
            btn_row, text="SELF-PWR", font=self.fn_hdr,
            bg=C["panel2"], fg=C["text"], relief="flat", bd=0,
            cursor="hand2", padx=8, pady=5,
            command=lambda: self._cmd_mode("self_powered")
        )
        self.btn_selfpow.pack(side="left", expand=True, fill="x", padx=(3, 0))

        # ── Battery Charge ───────────────────────────────────────────────────
        sep("BATTERY CHARGE")

        self.lbl_batt_status = tk.Label(p, text="IDLE", bg=C["panel"],
                                         fg=C["dim"], font=self.fn_med)
        self.lbl_batt_status.pack(pady=(2, 2))

        tk.Label(p, text="CHARGE RATE (W)", bg=C["panel"],
                 fg=C["dim"], font=self.fn_lbl).pack(anchor="w", padx=12)

        self.charge_rate_var = tk.IntVar(value=1000)
        self._slider_timer = None  # debounce timer for auto-apply
        self.slider = tk.Scale(
            p, from_=600, to=12000, resolution=100,
            orient="horizontal", variable=self.charge_rate_var,
            bg=C["panel"], fg=C["text"], troughcolor=C["panel2"],
            highlightthickness=0, sliderrelief="flat",
            activebackground=C["amber"], font=self.fn_lbl,
            command=self._on_slider_change
        )
        self.slider.pack(fill="x", padx=10, pady=(0, 2))

        rate_row = tk.Frame(p, bg=C["panel"])
        rate_row.pack(fill="x", padx=10)
        self.lbl_rate = tk.Label(rate_row, text="1000 W", bg=C["panel"],
                                  fg=C["amber"], font=self.fn_med)
        self.lbl_rate.pack(side="left", expand=True)
        self.btn_apply_rate = tk.Button(
            rate_row, text="APPLY", font=self.fn_lbl,
            bg=C["panel2"], fg=C["amber"], relief="flat", bd=0,
            cursor="hand2", padx=8, pady=2,
            command=self._cmd_apply_rate
        )
        self.btn_apply_rate.pack(side="right")

        # Max charge SOC
        soc_row = tk.Frame(p, bg=C["panel"])
        soc_row.pack(fill="x", padx=10, pady=(4, 0))
        tk.Label(soc_row, text="MAX SOC %", bg=C["panel"],
                 fg=C["dim"], font=self.fn_lbl, anchor="w").pack(side="left")
        self.max_soc_var = tk.IntVar(value=100)
        self.max_soc_spin = tk.Spinbox(
            soc_row, from_=50, to=100, increment=5,
            textvariable=self.max_soc_var, width=5,
            bg=C["panel2"], fg=C["text"], font=self.fn_lbl,
            buttonbackground=C["panel2"], relief="flat",
        )
        self.max_soc_spin.pack(side="right")

        chg_row = tk.Frame(p, bg=C["panel"])
        chg_row.pack(fill="x", padx=10, pady=(4, 0))
        tk.Button(
            chg_row, text="▶ START CHARGE", font=self.fn_hdr,
            bg=C["green_dim"], fg=C["green"], relief="flat", bd=0,
            cursor="hand2", padx=8, pady=6,
            command=self._cmd_charge_start
        ).pack(fill="x", pady=(0, 3))
        tk.Button(
            chg_row, text="■ STOP CHARGE", font=self.fn_hdr,
            bg=C["red_dim"], fg=C["red"], relief="flat", bd=0,
            cursor="hand2", padx=8, pady=6,
            command=self._cmd_charge_stop
        ).pack(fill="x")

        # ── Live Readouts ────────────────────────────────────────────────────
        sep("LIVE READINGS")
        self.readout_vars = {}
        for key, label, unit in [
            ("grid_w",    "Grid Draw",  "W"),
            ("load_w",    "Home Load",  "W"),
            ("battery_w", "Battery",    "W"),
            ("soc_pct",   "SOC",        "%"),
            ("volt_a",    "Line A",     "V"),
            ("volt_b",    "Line B",     "V"),
        ]:
            row = tk.Frame(p, bg=C["panel"])
            row.pack(fill="x", padx=12, pady=1)
            tk.Label(row, text=label, bg=C["panel"], fg=C["dim"],
                     font=self.fn_sm, width=11, anchor="w").pack(side="left")
            var = tk.StringVar(value="—")
            lbl = tk.Label(row, textvariable=var, bg=C["panel"],
                           fg=C["text"], font=self.fn_sm, width=10, anchor="e")
            lbl.pack(side="right")
            self.readout_vars[key] = (var, lbl, unit)

        # ── Command Log ──────────────────────────────────────────────────────
        sep("COMMAND LOG")
        self.cmd_log = tk.Text(
            p, bg=C["bg"], fg=C["dim"], font=self.fn_lbl,
            height=6, wrap="word", state="disabled", relief="flat", padx=4, pady=4
        )
        self.cmd_log.pack(fill="x", padx=10, pady=(2, 10))

    def _thresh_row(self, parent, label, attr, lo, hi, unit="c", step=0.5):
        """One threshold row: label  [-]  value  [+]. Changes trigger automation re-eval."""
        row = tk.Frame(parent, bg=C["panel"])
        row.pack(fill="x", pady=1)
        tk.Label(row, text=label, bg=C["panel"], fg=C["dim"],
                 font=self.fn_lbl, width=14, anchor="w").pack(side="left")

        var = tk.StringVar()
        if unit == "W":
            fmt = lambda v: f"{int(v)}W"
        elif unit == "%":
            fmt = lambda v: f"{v:.0f}{unit}"
        else:
            fmt = lambda v: f"{v:.1f}{unit}"

        def refresh():
            var.set(fmt(getattr(self.thresholds, attr)))

        def _change(delta):
            v = getattr(self.thresholds, attr)
            setattr(self.thresholds, attr, round(max(lo, min(hi, v + delta)), 1))
            refresh()
            self.thresholds.save()
            # Trigger immediate automation re-evaluation
            if self.auto.enabled:
                threading.Thread(target=self._run_automation, daemon=True).start()

        refresh()
        tk.Button(row, text="-", font=self.fn_lbl, bg=C["panel2"], fg=C["text"],
                  relief="flat", bd=0, width=2, cursor="hand2",
                  command=lambda: _change(-step)).pack(side="left")
        tk.Label(row, textvariable=var, bg=C["panel"], fg=C["amber"],
                 font=self.fn_lbl, width=7).pack(side="left")
        tk.Button(row, text="+", font=self.fn_lbl, bg=C["panel2"], fg=C["text"],
                  relief="flat", bd=0, width=2, cursor="hand2",
                  command=lambda: _change(step)).pack(side="left")

        self._thresh_vars[attr] = refresh

    # ─────────────────────────────────────────────────────────────────────────
    # COMMAND HANDLERS
    # ─────────────────────────────────────────────────────────────────────────
    def _on_slider_change(self, val):
        """Called on every slider tick. Updates label + starts 5s debounce for auto-apply."""
        self.lbl_rate.config(text=f"{int(float(val))} W")
        # Cancel previous timer, start new 5s debounce
        if self._slider_timer is not None:
            self.root.after_cancel(self._slider_timer)
        self._slider_timer = self.root.after(5000, self._cmd_apply_rate)

    def _cmd_apply_rate(self):
        """Send charge power command (rate + max SOC) without toggling charge on/off."""
        self._slider_timer = None
        rate = self.charge_rate_var.get()
        max_soc = self.max_soc_var.get()
        self._log_cmd(f"SET RATE {rate}W  SOC<={max_soc}%")
        config_write = build_charge_power_command(rate, max_soc=max_soc)
        payload = _build_and_wrap(config_write)
        self.mqtt.publish_command(payload, self._commands_live)

    def _cmd_mode(self, mode: str):
        self._log_cmd(f"MODE -> {mode.upper()}")
        self_powered = (mode == "self_powered")
        config_write = build_mode_command(self_powered=self_powered)
        payload = _build_and_wrap(config_write)
        self.mqtt.publish_command(payload, self._commands_live)

    def _cmd_charge_start(self, rate: int = None):
        rate = rate or self.charge_rate_var.get()
        max_soc = self.max_soc_var.get()
        self._log_cmd(f"CHARGE START  {rate}W  SOC<={max_soc}%")
        # Send charge ON + power level + SOC limit as a single ConfigWrite
        config_write = (build_charge_command(enable=True)
                        + build_charge_power_command(rate, max_soc=max_soc))
        payload = _build_and_wrap(config_write)
        self.mqtt.publish_command(payload, self._commands_live)

    def _cmd_charge_stop(self):
        self._log_cmd("CHARGE STOP")
        config_write = build_charge_command(enable=False)
        payload = _build_and_wrap(config_write)
        self.mqtt.publish_command(payload, self._commands_live)

    def _toggle_commands(self):
        self._commands_live = not self._commands_live
        state = "LIVE" if self._commands_live else "DRY RUN"
        log.info("Commands toggled: %s", state)
        self._log_cmd(f"COMMANDS set to {state}")

    def _toggle_automation(self):
        self.auto.enabled = not self.auto.enabled
        self._log_cmd(f"AUTOMATION {'ON' if self.auto.enabled else 'OFF'}")

    def _log_cmd(self, text: str):
        ts     = time.strftime("%H:%M:%S")
        prefix = "[LIVE]" if self._commands_live else "[DRY] "
        self.cmd_log.config(state="normal")
        self.cmd_log.insert("end", f"{ts} {prefix} {text}\n")
        self.cmd_log.see("end")
        lines = int(self.cmd_log.index("end-1c").split(".")[0])
        if lines > 40:
            self.cmd_log.delete("1.0", "2.0")
        self.cmd_log.config(state="disabled")

    # ─────────────────────────────────────────────────────────────────────────
    # CALLBACKS & AUTOMATION
    # ─────────────────────────────────────────────────────────────────────────
    def _on_mqtt_update(self):
        with self._lock: self._dirty = True
        # Re-evaluate automation when power state changes (SOC may cross band boundary)
        if self.auto.enabled and self.price.effective_price is not None:
            threading.Thread(target=self._run_automation, daemon=True).start()

    def _on_price_update(self):
        with self._lock: self._dirty = True
        threading.Thread(target=self._run_automation, daemon=True).start()

    def _run_automation(self):
        try:
            mode, rate, reason = self.auto.decide(
                self.price, self.state, self.thresholds)
            self.auto.last_decision = reason

            ok, why = self.auto.should_send(mode, rate)
            if not ok:
                return

            self._log_cmd(f"AUTO: {reason}")

            if mode == 2:
                self._cmd_mode("self_powered")
            elif mode == 1 and self.state.op_mode != 1:
                # Send backup/charge command if not already in backup mode,
                # including when op_mode is None (telemetry not yet received)
                self._cmd_mode("backup")

            if rate == 0:
                self._cmd_charge_stop()
            elif rate is not None:
                self._cmd_charge_start(rate=rate)

            self.auto.record(mode, rate, reason)

        except Exception as e:
            log.warning("Automation error: %s", e)

    # ─────────────────────────────────────────────────────────────────────────
    # TICK & DRAWING
    # ─────────────────────────────────────────────────────────────────────────
    _last_quota_request = 0.0
    _last_auto_reeval   = 0.0

    def _tick(self):
        with self._lock: self._dirty = False
        now = time.time()
        # Re-request telemetry every 30s if data is stale
        if (self.mqtt.connected and self.state.stale
                and now - Dashboard._last_quota_request > 30):
            Dashboard._last_quota_request = now
            self.mqtt._request_quotas(self.mqtt._client)
        # Periodic automation re-evaluation every 30s (independent of data events)
        if (self.auto.enabled and now - Dashboard._last_auto_reeval > 30
                and self.price.effective_price is not None):
            Dashboard._last_auto_reeval = now
            threading.Thread(target=self._run_automation, daemon=True).start()
        self._update_topbar()
        self._update_price_panel()
        self._update_controls()
        self._update_readouts()
        self._draw_flow()
        self._draw_history()
        self.root.after(self.REFRESH_MS, self._tick)

    def _update_topbar(self):
        self.lbl_time.config(text=time.strftime("%H:%M:%S"))

        if self.mqtt.connected and not self.state.stale:
            self.lbl_status.config(text="● LIVE",       fg=C["green"])
        elif self.mqtt.connected:
            self.lbl_status.config(text="● CONNECTED",  fg=C["amber"])
        else:
            self.lbl_status.config(text="● CONNECTING…", fg=C["red"])

        # Commands toggle button
        if self._commands_live:
            self.btn_cmd_toggle.config(
                text="COMMANDS: LIVE", bg=C["green_dim"], fg=C["green"])
        else:
            self.btn_cmd_toggle.config(
                text="COMMANDS: DRY RUN", bg=C["amber_dim"], fg=C["amber"])

        self.lbl_mode_top.config(
            text=f"MODE: {self.state.mode_label}",
            fg=C["green"] if self.state.op_mode == 2 else C["blue"])

        ps = self.price
        if ps.effective_price is not None or ps.price_hour is not None or ps.price_5min is not None:
            arrow = {"rising": " ↑", "falling": " ↓", "flat": " →"}.get(ps.trend, "")
            primary = ps.effective_price or ps.price_hour or ps.price_5min
            _, top_color = _classify_price(primary)
            self.lbl_price_top.config(
                text=f"PRICE: {primary:.1f}c{arrow}", fg=top_color)
        else:
            self.lbl_price_top.config(text="PRICE: —", fg=C["dim"])

    def _update_price_panel(self):
        ps = self.price
        if ps.price_hour is not None:
            # Hour average is the primary (billing) number — show it big
            hour_tier, hour_color = _classify_price(ps.price_hour)
            self.lbl_price_big.config(text=f"{ps.price_hour:.1f}c", fg=hour_color)
            self.lbl_tier.config(text=f"{hour_tier}  (hour avg)", fg=hour_color)
        elif ps.price_5min is not None:
            hour_color = ps.tier_color
            self.lbl_price_big.config(text=f"{ps.price_5min:.1f}c", fg=ps.tier_color)
            self.lbl_tier.config(text=ps.tier, fg=ps.tier_color)
        else:
            note = f"({ps.error[:28]})" if ps.error else "(polling…)"
            self.lbl_price_big.config(text="—", fg=C["dim"])
            self.lbl_tier.config(text="—", fg=C["dim"])
            self.lbl_trend.config(text=note, fg=C["dim"])
            self.lbl_hour_avg.config(text="—", fg=C["dim"])
            self._draw_price_sparkline()
            return

        # Secondary info: 5-min price + running average
        if ps.price_5min is not None:
            arrow = {"rising": "↑ RISING", "falling": "↓ FALLING",
                     "flat":   "→ STABLE"}.get(ps.trend, "")
            self.lbl_trend.config(text=arrow, fg=ps.tier_color)
            parts = [f"5-min: {ps.price_5min:.1f}c"]
            if ps.running_hour_avg is not None:
                parts.append(f"Running: {ps.running_hour_avg:.1f}c")
            self.lbl_hour_avg.config(text="  |  ".join(parts), fg=ps.tier_color)
        else:
            self.lbl_trend.config(text="", fg=C["dim"])
            self.lbl_hour_avg.config(text="5-min: —", fg=C["dim"])

        self._draw_price_sparkline()

    def _draw_price_sparkline(self):
        c = self.price_canvas
        c.delete("all")
        W = c.winfo_width(); H = c.winfo_height()
        if W < 10 or H < 10: return

        entries = self.price.history_5min
        if len(entries) < 2: return

        # entries newest-first; reverse for left→right (chronological)
        vals = [p for _, p in reversed(entries)]
        n    = len(vals)
        lo   = min(vals) - 0.5
        hi   = max(vals) + 0.5
        rng  = max(hi - lo, 2.0)

        def px(i): return int(i / max(n - 1, 1) * (W - 6)) + 3
        def py(v): return int(H - 4 - (v - lo) / rng * (H - 8))

        # Zero line — visible when prices go negative
        if lo < 0 < hi:
            zy = py(0)
            c.create_line(0, zy, W, zy, fill=C["border"], width=1, dash=(2, 4))
            c.create_text(W - 2, zy - 1, text="0", fill=C["dim"],
                          font=self.fn_lbl, anchor="se")

        # Charge threshold lines (green dashed) — one per SOC band
        for t_chg, shade in [(self.thresholds.low_charge_below,  "#2a5a2a"),
                             (self.thresholds.mid_charge_below,  "#1a4a22"),
                             (self.thresholds.high_charge_below, "#0f3a15")]:
            if lo - 1 <= t_chg <= hi + 1:
                c.create_line(0, py(t_chg), W, py(t_chg),
                              fill=shade, width=1, dash=(3, 5))

        # Discharge threshold reference line (red dashed)
        thresh = self.thresholds.discharge_above
        if lo - 1 <= thresh <= hi + 1:
            ty = py(thresh)
            c.create_line(0, ty, W, ty, fill=C["red_dim"], width=1, dash=(3, 5))

        # Fixed rate reference line (amber dashed) — the break-even vs fixed plan
        if lo - 1 <= COMED_FIXED_RATE <= hi + 1:
            fy = py(COMED_FIXED_RATE)
            c.create_line(0, fy, W, fy, fill=C["amber_dim"], width=1, dash=(2, 6))

        # Price sparkline (actual data)
        pts  = [(px(i), py(v)) for i, v in enumerate(vals)]
        flat = [coord for pt in pts for coord in pt]
        if len(flat) >= 4:
            c.create_line(flat, fill=self.price.tier_color, width=2, smooth=True)

        # ── Linear regression trend line ─────────────────────────────────────
        # Compute slope and intercept over all points
        if n >= 3:
            xs    = list(range(n))
            xm    = (n - 1) / 2
            ym    = sum(vals) / n
            num   = sum((xs[i] - xm) * (vals[i] - ym) for i in range(n))
            den   = sum((xs[i] - xm) ** 2 for i in range(n))
            slope = num / den if den else 0.0
            # Predicted values at first and last point
            v0    = ym + slope * (0 - xm)
            v1    = ym + slope * ((n - 1) - xm)
            # Clamp to visible range for drawing (line extends full width)
            tx0, ty0 = px(0),     py(v0)
            tx1, ty1 = px(n - 1), py(v1)
            # Trend line color: green=falling (cheaper), red=rising (more expensive)
            if slope > 0.15:
                tline_color = "#f85149"    # rising → red
            elif slope < -0.15:
                tline_color = "#3fb950"    # falling → green
            else:
                tline_color = "#8b949e"    # flat → grey
            c.create_line(tx0, ty0, tx1, ty1,
                          fill=tline_color, width=2, dash=(6, 4))

        # Current price dot (end of sparkline)
        if pts:
            lx, ly = pts[-1]
            c.create_oval(lx-3, ly-3, lx+3, ly+3,
                          fill=self.price.tier_color, outline="")

    def _update_controls(self):
        s = self.state
        # Mode buttons
        if s.op_mode == 1:
            self.btn_backup.config( bg=C["blue_dim"],  fg=C["blue"])
            self.btn_selfpow.config(bg=C["panel2"],    fg=C["dim"])
        elif s.op_mode == 2:
            self.btn_selfpow.config(bg=C["green_dim"], fg=C["green"])
            self.btn_backup.config( bg=C["panel2"],    fg=C["dim"])
        else:
            self.btn_backup.config( bg=C["panel2"],    fg=C["dim"])
            self.btn_selfpow.config(bg=C["panel2"],    fg=C["dim"])

        # Battery status
        bw = s.battery_w or 0
        if bw > 50:
            self.lbl_batt_status.config(text=f"CHARGING  +{bw:.0f}W",   fg=C["green"])
        elif bw < -50:
            self.lbl_batt_status.config(text=f"DISCHARGING  {bw:.0f}W", fg=C["red"])
        else:
            self.lbl_batt_status.config(text="IDLE", fg=C["dim"])

        self.lbl_mode.config(
            text=s.mode_label,
            fg=C["green"] if s.op_mode == 2 else C["blue"])

        # Automation button
        if self.auto.enabled:
            self.btn_auto.config(text="AUTO: ON", bg=C["green_dim"], fg=C["green"])
            self.lbl_auto_status.config(text=self.auto.last_decision, fg=C["text"])
        else:
            self.btn_auto.config(text="AUTO: OFF", bg=C["panel2"], fg=C["dim"])
            self.lbl_auto_status.config(
                text="Enable to auto-control charging / mode", fg=C["dim"])

    def _update_readouts(self):
        s = self.state
        vals = {"grid_w": s.grid_w, "load_w": s.load_w, "battery_w": s.battery_w,
                "soc_pct": s.soc_pct, "volt_a": s.volt_a,  "volt_b": s.volt_b}
        for key, (var, lbl, unit) in self.readout_vars.items():
            v = vals[key]
            if v is None:
                var.set("—"); lbl.config(fg=C["dim"])
            else:
                if key == "battery_w":
                    var.set(f"{v:+.0f} W")
                elif key == "soc_pct":
                    var.set(f"{v:.0f}%")
                elif unit == "W":
                    var.set(f"{v:.0f} W")
                else:
                    var.set(f"{v:.1f} V")

                if key == "battery_w":
                    lbl.config(fg=C["green"] if v > 50 else (C["red"] if v < -50 else C["dim"]))
                elif key == "soc_pct":
                    lbl.config(fg=C["red"] if v < 20 else (C["amber"] if v < 50 else C["green"]))
                elif key == "grid_w":
                    lbl.config(fg=C["amber"] if (v or 0) > 100 else C["green"])
                else:
                    lbl.config(fg=C["text"])

    # ─────────────────────────────────────────────────────────────────────────
    # POWER FLOW DIAGRAM
    # ─────────────────────────────────────────────────────────────────────────
    def _draw_flow(self):
        c = self.flow_canvas
        c.delete("all")
        W = c.winfo_width(); H = c.winfo_height()
        if W < 10 or H < 10: return

        s = self.state
        cx, cy   = W / 2, H / 2
        r        = min(W, H) * 0.10
        gx, gy   = cx, cy
        grid_x, grid_y = cx * 0.38, cy * 0.42
        batt_x, batt_y = cx * 0.38, cy * 1.58
        load_x, load_y = cx * 1.62, cy

        for x in range(0, W, 40):
            c.create_line(x, 0, x, H, fill=C["grid_line"])
        for y in range(0, H, 40):
            c.create_line(0, y, W, y, fill=C["grid_line"])

        def flow(x1, y1, x2, y2, watts, color):
            if watts is None or abs(watts) < 10:
                c.create_line(x1, y1, x2, y2, fill=C["border"], width=2, dash=(4, 8))
                return
            spd = min(abs(watts) / 1000.0, 3.0)
            off = int(time.time() * spd * 20) % 20
            if watts < 0: x1, y1, x2, y2 = x2, y2, x1, y1
            c.create_line(x1, y1, x2, y2, fill=color, width=3, dash=(8, 12), dashoffset=off)
            dx, dy = x2 - x1, y2 - y1
            ln = math.hypot(dx, dy)
            if ln > 0:
                ux, uy = dx / ln, dy / ln
                ax, ay = x2 - ux * 18, y2 - uy * 18
                px2, py2 = -uy * 7, ux * 7
                c.create_polygon(x2, y2, ax+px2, ay+py2, ax-px2, ay-py2,
                                  fill=color, outline="")
            mx, my = (x1+x2)/2, (y1+y2)/2
            c.create_text(mx, my - 12, text=f"{abs(watts):.0f} W",
                          fill=color, font=self.fn_sm)

        gw = s.grid_w or 0; bw = s.battery_w or 0; lw = s.load_w or 0
        bc = C["green"] if bw > 50 else (C["red"] if bw < -50 else C["dim"])

        flow(grid_x, grid_y, gx, gy, gw, C["flow_grid"])
        flow(batt_x, batt_y, gx, gy, -bw, bc)
        flow(gx, gy, load_x, load_y, lw, C["flow_load"])

        def node(x, y, label, sub, color, icon):
            c.create_oval(x-r-8, y-r-8, x+r+8, y+r+8, fill="", outline=color, width=1)
            c.create_oval(x-r,   y-r,   x+r,   y+r,   fill=C["panel2"], outline=color, width=2)
            c.create_text(x, y-8,  text=icon,  fill=color, font=self.fn_med)
            c.create_text(x, y+10, text=label, fill=color, font=self.fn_lbl)
            c.create_text(x, y+22, text=sub,   fill=C["dim"], font=self.fn_lbl)

        gdir = "IMPORT" if gw > 0 else ("EXPORT" if gw < 0 else "IDLE")
        bdir = "CHG" if bw > 50 else ("DSG" if bw < -50 else "IDLE")
        soc_str = f"{s.soc_pct:.0f}%" if s.soc_pct is not None else ""
        batt_sub = f"{bdir}  {soc_str}" if soc_str else bdir
        node(grid_x, grid_y, "GRID",    gdir,          C["flow_grid"], "⚡")
        node(batt_x, batt_y, "BATTERY", batt_sub,      bc,             "🔋")
        node(load_x, load_y, "HOME",    f"{lw:.0f}W",  C["flow_load"], "🏠")

        # Gateway centre
        gwc = C["amber"] if not s.stale else C["dim"]
        r2  = r * 1.3
        c.create_oval(gx-r2-8, gy-r2-8, gx+r2+8, gy+r2+8, fill="", outline=gwc, width=1)
        c.create_oval(gx-r2,   gy-r2,   gx+r2,   gy+r2,   fill=C["panel2"], outline=gwc, width=2)
        c.create_text(gx, gy-10, text="⚙",       fill=gwc, font=self.fn_big)
        c.create_text(gx, gy+20, text="GATEWAY",  fill=gwc, font=self.fn_lbl)
        mc = C["green"] if s.op_mode == 2 else C["blue"]
        c.create_text(gx, gy+32, text=s.mode_label, fill=mc, font=self.fn_lbl)

        # ComEd price overlay — hour avg (billing) primary, 5-min secondary
        ps = self.price
        if ps.price_hour is not None or ps.price_5min is not None:
            arr = {"rising": "↑", "falling": "↓", "flat": "→"}.get(ps.trend, "")
            primary = ps.price_hour if ps.price_hour is not None else ps.price_5min
            _, ov_color = _classify_price(primary)
            line1 = f"ComEd: {primary:.1f}c {arr}"
            c.create_text(W-10, 10, text=line1,
                          fill=ov_color, font=self.fn_lbl, anchor="ne")
            if ps.price_5min is not None and ps.price_hour is not None:
                c.create_text(W-10, 22,
                              text=f"5-min: {ps.price_5min:.1f}c",
                              fill=C["dim"], font=self.fn_lbl, anchor="ne")

        if s.volt_a and s.volt_b:
            c.create_text(W-10, H-10,
                          text=f"L1 {s.volt_a:.1f}V  L2 {s.volt_b:.1f}V",
                          fill=C["dim"], font=self.fn_lbl, anchor="se")

    # ─────────────────────────────────────────────────────────────────────────
    # HISTORY GRAPH
    # ─────────────────────────────────────────────────────────────────────────
    def _draw_history(self):
        c = self.hist_canvas
        c.delete("all")
        W = c.winfo_width(); H = c.winfo_height()
        if W < 10 or H < 10: return

        PL, PR, PT, PB = 50, 12, 10, 30
        gw = W - PL - PR; gh = H - PT - PB

        c.create_rectangle(PL, PT, PL+gw, PT+gh, fill=C["bg"], outline=C["border"])

        hist = self.history
        if len(hist.times) < 2:
            c.create_text(PL+gw/2, PT+gh/2, text="Collecting data…",
                          fill=C["dim"], font=self.fn_lbl)
            return

        now  = time.time()
        t0   = now - HISTORY_SECONDS
        all_v = list(hist.grid) + list(hist.load) + list(hist.battery)
        vmin = min(all_v) if all_v else -500
        vmax = max(all_v) if all_v else 5000
        rng  = max(vmax - vmin, 500)
        vmin -= rng * 0.05; vmax += rng * 0.05

        def tx(t): return PL + (t - t0) / HISTORY_SECONDS * gw
        def ty(v): return PT + gh - (v - vmin) / (vmax - vmin) * gh

        for frac in [0.0, 0.25, 0.5, 0.75, 1.0]:
            y = PT + gh * (1 - frac)
            v = vmin + (vmax - vmin) * frac
            c.create_line(PL, y, PL+gw, y, fill=C["grid_line"])
            c.create_text(PL-4, y, text=f"{v:.0f}", fill=C["dim"],
                          font=self.fn_lbl, anchor="e")

        if vmin < 0 < vmax:
            c.create_line(PL, ty(0), PL+gw, ty(0), fill=C["border"], dash=(4, 4))

        for m in [0, 3, 6, 9, 12, 15]:
            x = tx(now - m * 60)
            if PL <= x <= PL+gw:
                c.create_text(x, PT+gh+12, text=f"-{m}m" if m else "now",
                              fill=C["dim"], font=self.fn_lbl, anchor="n")

        times = list(hist.times)

        def series(vals, color):
            pts  = [(tx(t), ty(v)) for t, v in zip(times, vals) if tx(t) >= PL]
            if len(pts) < 2: return
            c.create_line([coord for pt in pts for coord in pt],
                          fill=color, width=2, smooth=True)

        series(hist.grid,    C["flow_grid"])
        series(hist.load,    C["flow_load"])
        series(hist.battery, C["green"])

        # Clip
        for r2 in [(0, 0, PL-1, H), (PL+gw+1, 0, W, H),
                   (0, 0, W, PT-1), (0, PT+gh+1, W, H)]:
            c.create_rectangle(*r2, fill=C["panel"], outline=C["panel"])

    def run(self):
        self.root.mainloop()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print()
    print("  EcoFlow Energy Dashboard  v2.0")
    print("  Commands: DRY RUN  (use COMMANDS button in top bar to go live)")
    print()
    Dashboard().run()
