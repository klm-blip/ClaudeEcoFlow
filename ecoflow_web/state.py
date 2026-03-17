"""
Application state dataclasses and MQTT telemetry parser.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .proto_codec import ProtoDecoder

log = logging.getLogger("ecoflow")


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
    def battery_charging(self):
        return (self.battery_w or 0) > 50

    @property
    def battery_discharging(self):
        return (self.battery_w or 0) < -50

    @property
    def mode_label(self):
        return {1: "BACKUP", 2: "SELF-POWERED"}.get(self.op_mode, "\u2014")

    @property
    def stale(self):
        return (time.time() - self.last_update) > 10

    def to_dict(self):
        return {
            "grid_w":    self.grid_w,
            "load_w":    self.load_w,
            "battery_w": self.battery_w,
            "soc_pct":   self.soc_pct,
            "volt_a":    self.volt_a,
            "volt_b":    self.volt_b,
            "op_mode":   self.op_mode,
            "mode_label": self.mode_label,
            "stale":     self.stale,
        }


@dataclass
class PriceState:
    price_5min:       Optional[float] = None
    price_hour:       Optional[float] = None
    running_hour_avg: Optional[float] = None
    effective_price:  Optional[float] = None
    hour_prices:      list            = field(default_factory=list)
    _current_hour:    int             = -1
    trend:            str             = "flat"
    trend_slope:      float           = 0.0
    tier:             str             = "\u2014"
    tier_color:       str             = "#8b949e"
    history_5min:     list            = field(default_factory=list)
    last_update:      float           = 0.0
    error:            str             = ""

    @property
    def stale(self):
        return self.last_update > 0 and (time.time() - self.last_update) > 180

    def to_dict(self):
        return {
            "price_5min":       self.price_5min,
            "price_hour":       self.price_hour,
            "running_hour_avg": self.running_hour_avg,
            "effective_price":  self.effective_price,
            "trend":            self.trend,
            "trend_slope":      self.trend_slope,
            "tier":             self.tier,
            "tier_color":       self.tier_color,
            "history_5min":     self.history_5min,
            "error":            self.error,
        }


def parse_payload(payload: bytes, state: PowerState) -> bool:
    """Parse MQTT telemetry protobuf into PowerState. Returns True if any field updated."""
    try:
        outer   = ProtoDecoder.decode_message(payload)
        level1  = outer.get(1)
        if not isinstance(level1, dict):
            return False
        nested1 = level1.get("_nested", {})
        inner   = nested1.get(1)
        data    = inner.get("_nested", {}) if isinstance(inner, dict) else nested1
        if not data:
            return False

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
