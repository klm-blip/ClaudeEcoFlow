"""
Battery cost pool: two-layer FIFO model with AC/DC efficiency tracking.

Legacy layer (estimated from initial SOC) is consumed first on discharge,
so the pool transitions to purely observed data as quickly as possible.
"""

import json
import logging
import time

from .config import BATTERY_CAPACITY_WH

log = logging.getLogger("ecoflow")

DEFAULT_LEGACY_COST = 10.5  # cents/kWh — estimated cost of energy already in battery


class BatteryCostPool:

    def __init__(self, capacity_wh: int = BATTERY_CAPACITY_WH):
        self.capacity_wh = capacity_wh

        # Two-layer FIFO pool
        self.legacy_wh = 0.0
        self.legacy_cost_cents = 0.0
        self.observed_wh = 0.0
        self.observed_cost_cents = 0.0

        # Efficiency tracker — per charge session
        self._charging = False
        self._session_meter_wh = 0.0    # energy measured at battery meter during session
        self._session_soc_start = None  # SOC% when charge session began

        # Rolling efficiency estimate
        self.efficiency_pct = 0.0       # 0 = not yet measured
        self._efficiency_total_in = 0.0
        self._efficiency_total_stored = 0.0

        # Timing
        self._last_ts = 0.0

    # ── Core update (called every telemetry tick ~5s) ─────────────────────

    def update(self, battery_w: float, effective_price: float,
               soc_pct: float = None):
        """Accumulate charge cost / drain on discharge. Returns actual dt used."""
        now = time.time()
        if self._last_ts <= 0:
            self._last_ts = now
            return 0.0
        dt = now - self._last_ts
        if dt <= 0 or dt > 60:          # skip bogus gaps (>60s = stale/restart)
            self._last_ts = now
            return 0.0
        self._last_ts = now

        if battery_w > 50:
            # ── Charging ──────────────────────────────────────────────
            energy_wh = battery_w * dt / 3600.0
            cost_cents = (energy_wh / 1000.0) * effective_price
            self.observed_wh += energy_wh
            self.observed_cost_cents += cost_cents

            # Efficiency session tracking
            if not self._charging:
                self._charging = True
                self._session_meter_wh = 0.0
                self._session_soc_start = soc_pct
            self._session_meter_wh += energy_wh

        elif battery_w < -50:
            # ── Discharging (FIFO: legacy first) ──────────────────────
            energy_wh = abs(battery_w) * dt / 3600.0
            remaining = energy_wh

            # Drain legacy first
            if self.legacy_wh > 0 and remaining > 0:
                drain = min(remaining, self.legacy_wh)
                frac = drain / self.legacy_wh if self.legacy_wh > 0 else 0
                self.legacy_cost_cents -= self.legacy_cost_cents * frac
                self.legacy_wh -= drain
                remaining -= drain

            # Then drain observed
            if self.observed_wh > 0 and remaining > 0:
                drain = min(remaining, self.observed_wh)
                frac = drain / self.observed_wh if self.observed_wh > 0 else 0
                self.observed_cost_cents -= self.observed_cost_cents * frac
                self.observed_wh -= drain
                remaining -= drain

            # End charge session if we were charging
            if self._charging:
                self._end_charge_session(soc_pct)

        else:
            # Idle — end charge session if one was active
            if self._charging:
                self._end_charge_session(soc_pct)

        # Clamp
        self.legacy_wh = max(0.0, self.legacy_wh)
        self.legacy_cost_cents = max(0.0, self.legacy_cost_cents)
        self.observed_wh = max(0.0, self.observed_wh)
        self.observed_cost_cents = max(0.0, self.observed_cost_cents)

        total = self.legacy_wh + self.observed_wh
        if total > self.capacity_wh:
            scale = self.capacity_wh / total
            self.legacy_wh *= scale
            self.legacy_cost_cents *= scale
            self.observed_wh *= scale
            self.observed_cost_cents *= scale

        # SOC reconciliation — if SOC near 0, reset pool
        if soc_pct is not None and soc_pct < 1.0:
            self.legacy_wh = 0.0
            self.legacy_cost_cents = 0.0
            self.observed_wh = 0.0
            self.observed_cost_cents = 0.0

        return dt

    # ── Efficiency tracking ───────────────────────────────────────────────

    def _end_charge_session(self, soc_pct: float = None):
        """Finalize a charge session and update rolling efficiency."""
        self._charging = False
        if (self._session_soc_start is not None
                and soc_pct is not None
                and self._session_meter_wh > 50):  # ignore tiny sessions
            soc_delta = soc_pct - self._session_soc_start
            if soc_delta > 0.5:  # meaningful charge
                stored_wh = (soc_delta / 100.0) * self.capacity_wh
                self._efficiency_total_in += self._session_meter_wh
                self._efficiency_total_stored += stored_wh
                if self._efficiency_total_in > 0:
                    self.efficiency_pct = (
                        self._efficiency_total_stored
                        / self._efficiency_total_in * 100.0
                    )
                log.info(
                    "Charge session: %.0f Wh metered, %.1f%% SOC gained "
                    "(%.0f Wh stored), rolling efficiency %.1f%%",
                    self._session_meter_wh, soc_delta, stored_wh,
                    self.efficiency_pct,
                )
        self._session_meter_wh = 0.0
        self._session_soc_start = None

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def total_wh(self) -> float:
        return self.legacy_wh + self.observed_wh

    @property
    def total_cost_cents(self) -> float:
        return self.legacy_cost_cents + self.observed_cost_cents

    @property
    def avg_cost_cents_kwh(self) -> float:
        total = self.total_wh
        if total < 1.0:
            return 0.0
        return self.total_cost_cents / (total / 1000.0)

    @property
    def legacy_remaining_pct(self) -> float:
        """How much of pool is still legacy (estimated) vs observed."""
        total = self.total_wh
        if total < 1.0:
            return 0.0
        return self.legacy_wh / total * 100.0

    # ── Init from SOC ─────────────────────────────────────────────────────

    def initialize_from_soc(self, soc_pct: float,
                            cost_cents_kwh: float = DEFAULT_LEGACY_COST):
        """Set legacy layer from current SOC on first run."""
        self.legacy_wh = (soc_pct / 100.0) * self.capacity_wh
        self.legacy_cost_cents = (self.legacy_wh / 1000.0) * cost_cents_kwh
        log.info(
            "Battery pool initialized from SOC %.0f%%: %.0f Wh legacy at %.1f¢/kWh",
            soc_pct, self.legacy_wh, cost_cents_kwh,
        )

    # ── Serialization ─────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "legacy_wh": round(self.legacy_wh, 1),
            "legacy_cost_cents": round(self.legacy_cost_cents, 2),
            "observed_wh": round(self.observed_wh, 1),
            "observed_cost_cents": round(self.observed_cost_cents, 2),
            "total_wh": round(self.total_wh, 1),
            "total_kwh": round(self.total_wh / 1000.0, 2),
            "avg_cost_cents_kwh": round(self.avg_cost_cents_kwh, 1),
            "legacy_remaining_pct": round(self.legacy_remaining_pct, 1),
            "efficiency_pct": round(self.efficiency_pct, 1),
        }

    def save_state(self) -> dict:
        """Return dict for persistence in ecoflow_state.json."""
        return {
            "legacy_wh": self.legacy_wh,
            "legacy_cost_cents": self.legacy_cost_cents,
            "observed_wh": self.observed_wh,
            "observed_cost_cents": self.observed_cost_cents,
            "efficiency_total_in": self._efficiency_total_in,
            "efficiency_total_stored": self._efficiency_total_stored,
            "efficiency_pct": self.efficiency_pct,
        }

    def load_state(self, d: dict):
        """Restore from persisted state."""
        if not d:
            return False
        self.legacy_wh = d.get("legacy_wh", 0.0)
        self.legacy_cost_cents = d.get("legacy_cost_cents", 0.0)
        self.observed_wh = d.get("observed_wh", 0.0)
        self.observed_cost_cents = d.get("observed_cost_cents", 0.0)
        self._efficiency_total_in = d.get("efficiency_total_in", 0.0)
        self._efficiency_total_stored = d.get("efficiency_total_stored", 0.0)
        self.efficiency_pct = d.get("efficiency_pct", 0.0)
        log.info(
            "Battery pool loaded: legacy %.0f Wh + observed %.0f Wh = %.0f Wh, "
            "avg %.1f¢/kWh, efficiency %.1f%%",
            self.legacy_wh, self.observed_wh, self.total_wh,
            self.avg_cost_cents_kwh, self.efficiency_pct,
        )
        return True
