"""
Automation: SOC-tiered charge/discharge controller with floor-based band model.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, asdict

from .config import THRESHOLDS_FILE
from .state import PriceState, PowerState

log = logging.getLogger("ecoflow")


@dataclass
class AutoThresholds:
    # Discharge: switch to Self-Powered (battery powers home) above this price
    discharge_above:    float = 8.0    # cents/kWh

    # SOC-tiered charging with FLOOR model:
    #   Emergency (0% -> low_floor):           charge below emergency_charge_below
    #   Low       (low_floor -> mid_floor):    charge below low_charge_below
    #   Mid       (mid_floor -> high_floor):   charge below mid_charge_below
    #   High      (high_floor -> max_soc):     charge below high_charge_below
    #   Above max_soc:                         stop charging

    low_floor:          float = 20.0   # % - below this is emergency
    low_charge_below:   float = 2.0    # cents - generous (battery is low)
    low_rate:           int   = 6000   # W - charge fast

    mid_floor:          float = 60.0   # % - below this is low band
    mid_charge_below:   float = 1.5    # cents - moderate
    mid_rate:           int   = 3000   # W

    high_floor:         float = 85.0   # % - below this is mid band
    high_charge_below:  float = -1.0   # cents - only very cheap/negative
    high_rate:          int   = 1500   # W

    max_soc:            float = 95.0   # % - stop charging above this

    rate_emergency:     int   = 6000   # W - fast charge in emergency
    emergency_charge_below: float = 6.0  # cents - even emergency has a price cap

    def save(self):
        """Persist current thresholds to JSON file."""
        try:
            with open(THRESHOLDS_FILE, "w") as f:
                json.dump(asdict(self), f, indent=2)
        except Exception as e:
            log.warning("Failed to save thresholds: %s", e)

    @classmethod
    def load(cls):
        """Load thresholds from JSON file, falling back to defaults."""
        t = cls()
        if os.path.exists(THRESHOLDS_FILE):
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

    def to_dict(self):
        return asdict(self)


class AutoController:
    MIN_HOLD       = 30    # seconds between auto commands
    OVERRIDE_SECS  = 300   # 5 minutes — pause automation after manual mode change

    def __init__(self):
        self.enabled              = False
        self.last_mode            = None
        self.last_rate            = None
        self.last_cmd_ts          = 0.0
        self.last_decision        = "\u2014"
        self.manual_override_until = 0.0   # timestamp until which auto is paused

    def manual_mode_change(self, mode_int: int, override_minutes: int = None):
        """Call when the user manually changes mode via the UI.
        Syncs last_mode so automation knows the real state, and pauses
        automation for override_minutes (default OVERRIDE_SECS/60) so it
        doesn't immediately undo the change."""
        self.last_mode = mode_int
        secs = (override_minutes * 60) if override_minutes is not None else self.OVERRIDE_SECS
        self.manual_override_until = time.time() + secs
        log.info("Manual override: mode=%d, automation paused for %ds", mode_int, secs)

    def cancel_override(self):
        """Cancel manual override, letting automation resume immediately."""
        self.manual_override_until = 0.0
        log.info("Manual override cancelled")

    def decide(self, ps: PriceState, pw: PowerState, t: AutoThresholds):
        """
        Returns (target_mode, target_rate_w, reason). None = no change.

        SOC floor model - bands read bottom-up:
          Emergency (0% -> low_floor):         charge below emergency_charge_below
          Low       (low_floor -> mid_floor):  charge below low_charge_below
          Mid       (mid_floor -> high_floor): charge below mid_charge_below
          High      (high_floor -> max_soc):   charge below high_charge_below
          Above max_soc:                       stop charging
        """
        soc = pw.soc_pct

        # Grid outage detection: battery discharging in Backup mode with grid ~0W.
        # Don't try to charge or switch modes during an actual outage.
        if (pw.op_mode == 1
                and pw.battery_w is not None and pw.battery_w < -50
                and (pw.grid_w is None or pw.grid_w < 20)):
            return None, None, "OUTAGE: grid down, battery powering home \u2014 skipping automation"

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

        # Battery full
        if soc is not None and soc >= t.max_soc:
            if ep >= t.discharge_above:
                return 2, 0, f"DISCHARGE: full + {ep:.1f}c [{src}] >= {t.discharge_above:.1f}c"
            return 1, 0, f"HOLD: battery full ({soc:.0f}% >= {t.max_soc:.0f}%)"

        # High price -> self-powered (discharge)
        if ep >= t.discharge_above:
            if soc is None or soc > t.low_floor:
                return 2, 0, f"DISCHARGE: {ep:.1f}c [{src}] >= {t.discharge_above:.1f}c"
            return 1, 0, f"HOLD: price high but SOC {soc:.0f}% too low"

        # SOC-tiered charging decision (floor model)
        if soc is None:
            if ep < t.mid_charge_below:
                return 1, t.mid_rate, f"CHARGE: {ep:.1f}c [{src}] (no SOC, mid default)"
            return 1, 0, f"HOLD: {ep:.1f}c [{src}] (no SOC)"

        if soc < t.low_floor:
            if ep < t.emergency_charge_below:
                return 1, t.rate_emergency, f"EMERGENCY: SOC {soc:.0f}% < {t.low_floor:.0f}%  {ep:.1f}c < {t.emergency_charge_below:.1f}c [{src}]"
            return 1, 0, f"HOLD EMERGENCY: {ep:.1f}c [{src}] >= {t.emergency_charge_below:.1f}c  SOC {soc:.0f}%"

        if soc < t.mid_floor:
            if ep < t.low_charge_below:
                return 1, t.low_rate, f"CHARGE LOW: {ep:.1f}c [{src}] < {t.low_charge_below:.1f}c  SOC {soc:.0f}%"
            return 1, 0, f"HOLD LOW: {ep:.1f}c [{src}] >= {t.low_charge_below:.1f}c  SOC {soc:.0f}%"

        if soc < t.high_floor:
            if ep < t.mid_charge_below:
                return 1, t.mid_rate, f"CHARGE MID: {ep:.1f}c [{src}] < {t.mid_charge_below:.1f}c  SOC {soc:.0f}%"
            return 1, 0, f"HOLD MID: {ep:.1f}c [{src}] >= {t.mid_charge_below:.1f}c  SOC {soc:.0f}%"

        # HIGH band
        if ep < t.high_charge_below:
            return 1, t.high_rate, f"CHARGE HIGH: {ep:.1f}c [{src}] < {t.high_charge_below:.1f}c  SOC {soc:.0f}%"
        return 1, 0, f"HOLD HIGH: {ep:.1f}c [{src}] >= {t.high_charge_below:.1f}c  SOC {soc:.0f}%"

    def should_send(self, target_mode, target_rate):
        if not self.enabled:
            return False, "automation off"
        # Manual override active?
        now = time.time()
        if now < self.manual_override_until:
            remaining = int(self.manual_override_until - now)
            mins, secs = divmod(remaining, 60)
            return False, f"manual override ({mins}m{secs:02d}s)"
        mode_changed = target_mode is not None and target_mode != self.last_mode
        rate_changed = target_rate is not None and target_rate != self.last_rate
        if not mode_changed and not rate_changed:
            return False, "no change"
        if self.last_cmd_ts > 0:
            elapsed = now - self.last_cmd_ts
            if elapsed < self.MIN_HOLD:
                return False, f"hold {self.MIN_HOLD - elapsed:.0f}s"
        return True, "ok"

    def record(self, target_mode, target_rate, reason):
        if target_mode is not None:
            self.last_mode = target_mode
        if target_rate is not None:
            self.last_rate = target_rate
        self.last_cmd_ts   = time.time()
        self.last_decision = reason
