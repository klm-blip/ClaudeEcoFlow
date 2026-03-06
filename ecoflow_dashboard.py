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

import json
import logging
import math
import os as _os
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
MQTT_HOST   = "mqtt.ecoflow.com"
MQTT_PORT   = 8883
GATEWAY_SN  = "HR65ZA1AVH7J0027"
INVERTER_SN = "P101ZA1A9HA70164"

# Set True once you've verified commands work — START WITH False
ENABLE_COMMANDS = False

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
# SESSION_ID = 3rd segment of CLIENT_ID (e.g. "1971363830522871810")
# This is the routing ID in topics: /app/{SESSION_ID}/{device}/set
_id_parts  = CLIENT_ID.split("_", 2)
SESSION_ID = _id_parts[2] if len(_id_parts) >= 3 else _id_parts[-1]

TELEMETRY_TOPICS = [
    f"/app/device/property/{GATEWAY_SN}",
    f"/app/device/property/{INVERTER_SN}",
]
COMMAND_TOPIC = f"/app/{SESSION_ID}/{GATEWAY_SN}/set"


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
class ProtoEncoder:
    """
    Builds protobuf-encoded command messages for the EcoFlow HR65 gateway.
    The command structure mirrors the telemetry nesting:
      outer(field1) -> inner(field1=payload, field2=seq, field3=19)
        payload: field1=device_sn, field2=cmd_func, field3=seq, field4=pdata
    
    cmd_func codes confirmed for HR65:
      20 = AC charge config  (pdata: field1=watts, field2=pause_flag)
      32 = Work mode         (pdata: field1=mode  1=backup 2=self-powered)
              NOTE: cmd_func=32 (work_mode) not yet verified with corrected topic.
    """

    @staticmethod
    def _varint(v: int) -> bytes:
        out = []
        while True:
            out.append(v & 0x7F)
            v >>= 7
            if v == 0: break
        for i in range(len(out) - 1):
            out[i] |= 0x80
        return bytes(out)

    @staticmethod
    def _field(num: int, wire: int, val) -> bytes:
        e = ProtoEncoder
        tag = e._varint((num << 3) | wire)
        if wire == 0: return tag + e._varint(val)
        if wire == 2: return tag + e._varint(len(val)) + val
        if wire == 5: return tag + struct.pack('<f', val)
        raise ValueError(f"Unknown wire type {wire}")

    @classmethod
    def _str(cls, num, s):   return cls._field(num, 2, s.encode())
    @classmethod
    def _int(cls, num, v):   return cls._field(num, 0, v)
    @classmethod
    def _msg(cls, num, b):   return cls._field(num, 2, b)

    @classmethod
    def charge_config(cls, device_sn: str, watts: int, pause: int = 0,
                      seq: int = 1) -> bytes:
        """AC charge config command. pause=0 to start, pause=1 to stop."""
        pdata   = cls._int(1, watts) + cls._int(2, pause)
        payload = (cls._str(1, device_sn) + cls._int(2, 20) +
                   cls._int(3, seq)       + cls._msg(4, pdata))
        inner   = cls._msg(1, payload) + cls._int(2, seq) + cls._int(3, 19)
        return cls._msg(1, inner)

    @classmethod
    def work_mode(cls, device_sn: str, mode: int, seq: int = 1) -> bytes:
        """Work mode command. mode=1 backup, mode=2 self-powered."""
        pdata   = cls._int(1, mode)
        payload = (cls._str(1, device_sn) + cls._int(2, 32) +
                   cls._int(3, seq)       + cls._msg(4, pdata))
        # BUG FIX: outer seq must match inner seq (was hardcoded 1 before)
        inner   = cls._msg(1, payload) + cls._int(2, seq) + cls._int(3, 19)
        return cls._msg(1, inner)

    @classmethod
    def build(cls, device_sn: str, cmd_func: int, pdata: bytes,
              seq: int = 1) -> bytes:
        """Generic command builder — for trying alternate cmd_func codes."""
        payload = (cls._str(1, device_sn) + cls._int(2, cmd_func) +
                   cls._int(3, seq)       + cls._msg(4, pdata))
        inner   = cls._msg(1, payload) + cls._int(2, seq) + cls._int(3, 19)
        return cls._msg(1, inner)


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
    price_5min:   Optional[float] = None   # cents/kWh, most recent 5-min interval
    price_hour:   Optional[float] = None   # cents/kWh, current hour average
    trend:        str             = "flat" # "rising" | "falling" | "flat"
    trend_slope:  float           = 0.0
    tier:         str             = "—"
    tier_color:   str             = "#8b949e"
    history_5min: list            = field(default_factory=list)  # [(ts, price), ...] newest first
    last_update:  float           = 0.0
    error:        str             = ""

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

            log.info("ComEd: 5min=%.1f¢  hour=%s¢  trend=%s(%.2f)  tier=%s",
                     current,
                     f"{hour_avg:.1f}" if hour_avg else "?",
                     trend, sl, tier)
            self.on_update()

        except Exception as e:
            self.ps.error = str(e)
            log.warning("ComEd poll failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# AUTOMATION THRESHOLDS & CONTROLLER
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class AutoThresholds:
    discharge_above:    float = 9.6    # switch to Self-Powered above this (¢)
    hold_above:         float = 6.0    # stop charging above this (¢)
    charge_normal:      float = 6.0    # charge at normal rate below this (¢)
    charge_aggressive:  float = 3.0    # charge at max rate below this (¢)
    soc_emergency:      float = 20.0   # always charge regardless of price (%)
    soc_target:         float = 80.0   # normal charge target (%)
    soc_topoff:         float = 95.0   # only top-off at very cheap prices (%)
    rate_normal:        int   = 1500   # normal charge rate (W)
    rate_aggressive:    int   = 3000   # max charge rate (W)
    rate_emergency:     int   = 1000   # emergency charge rate (W)
    trend_lookahead:    bool  = True   # pre-act on strong trends


class AutoController:
    MIN_HOLD = 120   # seconds between commands

    def __init__(self):
        self.enabled        = False
        self.last_mode      = None
        self.last_rate      = None
        self.last_cmd_ts    = 0.0
        self.last_decision  = "—"

    def decide(self, ps: PriceState, pw: PowerState, t: AutoThresholds):
        """
        Returns (target_mode, target_rate_w, reason). None = no change.

        Primary signal: current hour average — this is what you're billed.
        Trend lookahead: 5-min slope nudges the effective price to pre-act
        before the hour average catches up to a move. Weight is halved when
        we have a solid hour average (less aggressive lookahead vs raw 5-min).
        Early in a new hour when hour_avg isn't meaningful yet, falls back to
        5-min price only.
        """
        soc = pw.soc_pct

        # Pick base price — prefer hour average (billing signal)
        if ps.price_hour is not None:
            base = ps.price_hour
            src  = "hr"
        elif ps.price_5min is not None:
            base = ps.price_5min
            src  = "5m"
        else:
            return None, None, "waiting for price data"

        # Trend lookahead using 5-min slope — nudge effective price if
        # 5-min is trending strongly away from current hour average.
        # Halved weight when we have a real hour average (less hair-trigger).
        ep = base
        if t.trend_lookahead and ps.price_5min is not None and abs(ps.trend_slope) > 1.0:
            weight = 0.5 if src == "hr" else 1.0
            ep = max(-5.0, base + ps.trend_slope * 2 * weight)

        # Emergency SOC — charge regardless of price
        if soc is not None and soc < t.soc_emergency:
            return 1, t.rate_emergency, f"EMERGENCY: SOC {soc:.0f}% < {t.soc_emergency:.0f}%"

        # Battery full — just decide on mode
        if soc is not None and soc >= t.soc_topoff:
            if ep >= t.discharge_above:
                return 2, 0, f"DISCHARGE: full + {ep:.1f}c [{src}] >= {t.discharge_above:.1f}c"
            return 1, 0, f"HOLD: battery full ({soc:.0f}%)"

        # High price → self-powered (discharge)
        if ep >= t.discharge_above:
            if soc is None or soc > t.soc_emergency + 10:
                return 2, 0, f"DISCHARGE: {ep:.1f}c [{src}] >= {t.discharge_above:.1f}c"
            return 1, 0, f"HOLD: price high but SOC {soc:.0f}% too low"

        # Hold band — don't charge
        if ep >= t.hold_above:
            return 1, 0, f"HOLD: {ep:.1f}c [{src}] in hold band"

        # Aggressive charging (very cheap)
        if ep < t.charge_aggressive:
            if soc is None or soc < t.soc_topoff:
                label = "TOPOFF" if (soc and soc >= t.soc_target) else "CHARGE MAX"
                return 1, t.rate_aggressive, f"{label}: {ep:.1f}c [{src}] deeply cheap"

        # Normal charging
        if ep < t.charge_normal:
            if soc is None or soc < t.soc_target:
                return 1, t.rate_normal, f"CHARGE: {ep:.1f}c [{src}] < {t.charge_normal:.1f}c"
            return 1, 0, f"HOLD: SOC {soc:.0f}% at target"

        return 1, 0, f"HOLD: {ep:.1f}c [{src}]"

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
        else:
            log.error("MQTT connect failed rc=%d", rc)

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
        self._client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
        self._client.loop_start()

    def publish_command(self, payload: bytes, commands_live: bool = False):
        """Send a protobuf-encoded command. payload must be bytes from ProtoEncoder."""
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
        self.thresholds = AutoThresholds()
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

        # Threshold adjusters
        thresh_f = tk.Frame(p, bg=C["panel"])
        thresh_f.pack(fill="x", padx=10)
        self._thresh_row(thresh_f, "Discharge >=",  "discharge_above",   1.0, 30.0)
        self._thresh_row(thresh_f, "Stop chg >=",   "hold_above",       -5.0, 20.0)
        self._thresh_row(thresh_f, "Chg normal <",  "charge_normal",    -5.0, 15.0)
        self._thresh_row(thresh_f, "Chg max <",     "charge_aggressive",-5.0, 10.0)

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
        self.slider = tk.Scale(
            p, from_=500, to=7200, resolution=100,
            orient="horizontal", variable=self.charge_rate_var,
            bg=C["panel"], fg=C["text"], troughcolor=C["panel2"],
            highlightthickness=0, sliderrelief="flat",
            activebackground=C["amber"], font=self.fn_lbl,
            command=lambda v: self.lbl_rate.config(text=f"{int(float(v))} W")
        )
        self.slider.pack(fill="x", padx=10, pady=(0, 2))

        self.lbl_rate = tk.Label(p, text="1000 W", bg=C["panel"],
                                  fg=C["amber"], font=self.fn_med)
        self.lbl_rate.pack()

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

    def _thresh_row(self, parent, label, attr, lo, hi):
        """One threshold row: label  [-]  value  [+]"""
        row = tk.Frame(parent, bg=C["panel"])
        row.pack(fill="x", pady=1)
        tk.Label(row, text=label, bg=C["panel"], fg=C["dim"],
                 font=self.fn_lbl, width=14, anchor="w").pack(side="left")

        var = tk.StringVar()

        def refresh():
            var.set(f"{getattr(self.thresholds, attr):.1f}c")

        def decr():
            v = getattr(self.thresholds, attr)
            setattr(self.thresholds, attr, round(max(lo, v - 0.5), 1))
            refresh()

        def incr():
            v = getattr(self.thresholds, attr)
            setattr(self.thresholds, attr, round(min(hi, v + 0.5), 1))
            refresh()

        refresh()
        tk.Button(row, text="-", font=self.fn_lbl, bg=C["panel2"], fg=C["text"],
                  relief="flat", bd=0, width=2, cursor="hand2",
                  command=decr).pack(side="left")
        tk.Label(row, textvariable=var, bg=C["panel"], fg=C["amber"],
                 font=self.fn_lbl, width=7).pack(side="left")
        tk.Button(row, text="+", font=self.fn_lbl, bg=C["panel2"], fg=C["text"],
                  relief="flat", bd=0, width=2, cursor="hand2",
                  command=incr).pack(side="left")

        self._thresh_vars[attr] = refresh

    # ─────────────────────────────────────────────────────────────────────────
    # COMMAND HANDLERS
    # ─────────────────────────────────────────────────────────────────────────
    def _cmd_mode(self, mode: str):
        m = 1 if mode == "backup" else 2
        seq = int(time.time()) & 0xFFFF
        self._log_cmd(f"MODE -> {mode.upper()}  (mode={m}, seq={seq})")
        # Try gateway SN first; if no response try inverter SN in next iteration
        payload = ProtoEncoder.work_mode(GATEWAY_SN, m, seq=seq)
        self.mqtt.publish_command(payload, self._commands_live)

    def _cmd_charge_start(self, rate: int = None):
        rate = rate or self.charge_rate_var.get()
        seq  = int(time.time()) & 0xFFFF
        self._log_cmd(f"CHARGE START  {rate} W  (seq={seq})")
        payload = ProtoEncoder.charge_config(GATEWAY_SN, rate, pause=0, seq=seq)
        self.mqtt.publish_command(payload, self._commands_live)

    def _cmd_charge_stop(self):
        self._log_cmd("CHARGE STOP")
        payload = ProtoEncoder.charge_config(GATEWAY_SN, 0, pause=1,
                                             seq=int(time.time()) & 0xFFFF)
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
        # Re-evaluate automation when power state changes (SOC thresholds)
        if self.auto.enabled and self.price.price_hour is not None:
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
            elif mode == 1 and self.state.op_mode == 2:
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
    def _tick(self):
        with self._lock: self._dirty = False
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
        if ps.price_hour is not None or ps.price_5min is not None:
            arrow = {"rising": " ↑", "falling": " ↓", "flat": " →"}.get(ps.trend, "")
            # Top bar shows hour avg (billing price) with trend arrow
            primary = ps.price_hour if ps.price_hour is not None else ps.price_5min
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

        # 5-min price is the secondary (real-time) number
        if ps.price_5min is not None:
            arrow = {"rising": "↑ RISING", "falling": "↓ FALLING",
                     "flat":   "→ STABLE"}.get(ps.trend, "")
            self.lbl_trend.config(text=arrow, fg=ps.tier_color)
            self.lbl_hour_avg.config(
                text=f"5-min: {ps.price_5min:.1f}c  ({ps.tier})",
                fg=ps.tier_color)
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

        # Charge-max threshold line (green dashed) — fires below this
        t_agg = self.thresholds.charge_aggressive
        if lo - 1 <= t_agg <= hi + 1:
            c.create_line(0, py(t_agg), W, py(t_agg),
                          fill=C["green_dim"], width=1, dash=(3, 5))

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
                "volt_a": s.volt_a,  "volt_b": s.volt_b}
        for key, (var, lbl, unit) in self.readout_vars.items():
            v = vals[key]
            if v is None:
                var.set("—"); lbl.config(fg=C["dim"])
            else:
                var.set(f"{v:+.0f} W" if key == "battery_w" else
                        f"{v:.0f} W"  if unit == "W" else f"{v:.1f} V")
                if key == "battery_w":
                    lbl.config(fg=C["green"] if v > 50 else (C["red"] if v < -50 else C["dim"]))
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
        node(grid_x, grid_y, "GRID",    gdir,          C["flow_grid"], "⚡")
        node(batt_x, batt_y, "BATTERY", bdir,          bc,             "🔋")
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
