"""
Battery cost pool: two-layer FIFO model with bidirectional efficiency tracking.

Tracks both charge (AC→DC) and discharge (DC→AC) efficiency separately,
giving a true roundtrip efficiency for profitability calculations.

Legacy layer (estimated from initial SOC) is consumed first on discharge,
so the pool transitions to purely observed data as quickly as possible.
"""

import logging
import time

from .config import BATTERY_CAPACITY_WH

log = logging.getLogger("ecoflow")

DEFAULT_LEGACY_COST = 11.5  # cents/kWh — estimated total cost incl T&D + ~8% AC/DC loss


class BatteryCostPool:

    def __init__(self, capacity_wh: int = BATTERY_CAPACITY_WH):
        self.capacity_wh = capacity_wh

        # Two-layer FIFO pool
        self.legacy_wh = 0.0
        self.legacy_cost_cents = 0.0
        self.observed_wh = 0.0
        self.observed_cost_cents = 0.0

        # Charge efficiency tracker (AC→DC) — per charge session
        self._charging = False
        self._session_meter_wh = 0.0    # energy measured at battery meter during session
        self._session_soc_start = None  # SOC% when charge session began

        # Rolling charge efficiency estimate
        self.charge_efficiency_pct = 0.0       # 0 = not yet measured
        self._charge_total_in = 0.0            # total AC Wh consumed for charging
        self._charge_total_stored = 0.0        # total DC Wh stored (from SOC change)

        # Discharge efficiency tracker (DC→AC) — per discharge session
        self._discharging = False
        self._discharge_ac_wh = 0.0     # AC Wh output by inverter (|battery_w|) during session
        self._discharge_soc_start = None  # SOC% when discharge session began

        # Rolling discharge efficiency estimate
        self.discharge_efficiency_pct = 0.0    # 0 = not yet measured
        self._discharge_total_out = 0.0        # total DC Wh consumed from battery (SOC change)
        self._discharge_total_delivered = 0.0  # total AC Wh delivered to home

        # Legacy alias for backward compat (charge efficiency)
        self.efficiency_pct = 0.0

        # Timing
        self._last_ts = 0.0

    # ── Core update (called every telemetry tick ~5s) ─────────────────────

    def update(self, battery_w: float, effective_price: float,
               soc_pct: float = None, **kwargs):
        """Accumulate charge cost / drain on discharge. Returns actual dt used.

        Args:
            battery_w: Battery power (positive=charging, negative=discharging).
                       This is AC-side (measured at inverter/panel), so it already
                       reflects conversion losses in both directions.
            effective_price: Total price including T&D (cents/kWh).
            soc_pct: Current battery SOC percentage.
        """
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

            # Charge efficiency session tracking
            if not self._charging:
                self._charging = True
                self._session_meter_wh = 0.0
                self._session_soc_start = soc_pct
            self._session_meter_wh += energy_wh

            # End discharge session if we were discharging
            if self._discharging:
                self._end_discharge_session(soc_pct)

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

            # Discharge efficiency session tracking
            # Use |battery_w| (= energy_wh) as AC-side output, not load_w,
            # because load_w includes grid contribution when battery can't
            # cover full load.
            if not self._discharging:
                self._discharging = True
                self._discharge_ac_wh = 0.0
                self._discharge_soc_start = soc_pct
            self._discharge_ac_wh += energy_wh  # energy_wh = |battery_w| * dt / 3600

        else:
            # Idle — end any active sessions
            if self._charging:
                self._end_charge_session(soc_pct)
            if self._discharging:
                self._end_discharge_session(soc_pct)

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
        """Finalize a charge session and update rolling charge efficiency (AC→DC)."""
        self._charging = False
        if (self._session_soc_start is not None
                and soc_pct is not None
                and self._session_meter_wh > 500):  # ignore small sessions (<500 Wh)
            soc_delta = soc_pct - self._session_soc_start
            if soc_delta >= 10.0:  # need 10%+ SOC change (~5 kWh) — integer SOC noise dominates smaller deltas
                stored_wh = (soc_delta / 100.0) * self.capacity_wh
                self._charge_total_in += self._session_meter_wh
                self._charge_total_stored += stored_wh
                if self._charge_total_in > 0:
                    self.charge_efficiency_pct = (
                        self._charge_total_stored
                        / self._charge_total_in * 100.0
                    )
                    self.efficiency_pct = self.charge_efficiency_pct  # legacy alias
                log.info(
                    "Charge session: %.0f Wh metered, %.1f%% SOC gained "
                    "(%.0f Wh stored), rolling charge efficiency %.1f%%",
                    self._session_meter_wh, soc_delta, stored_wh,
                    self.charge_efficiency_pct,
                )
        self._session_meter_wh = 0.0
        self._session_soc_start = None

    def _end_discharge_session(self, soc_pct: float = None):
        """Finalize a discharge session and update rolling discharge efficiency (DC→AC).

        Measures: how many AC Wh did the inverter output (|battery_w| accumulation)
        vs how many DC Wh left the battery cells (SOC change × capacity).
        Efficiency = AC_out / DC_consumed — how much of the DC energy
        actually makes it to the home after inverter losses.
        """
        self._discharging = False
        if (self._discharge_soc_start is not None
                and soc_pct is not None
                and self._discharge_ac_wh > 500):  # ignore small sessions (<500 Wh)
            soc_delta = self._discharge_soc_start - soc_pct  # positive = SOC dropped
            if soc_delta >= 10.0:  # need 10%+ SOC change (~5 kWh) — integer SOC noise dominates smaller deltas
                consumed_wh = (soc_delta / 100.0) * self.capacity_wh
                self._discharge_total_out += consumed_wh
                self._discharge_total_delivered += self._discharge_ac_wh
                if self._discharge_total_out > 0:
                    self.discharge_efficiency_pct = (
                        self._discharge_total_delivered
                        / self._discharge_total_out * 100.0
                    )
                log.info(
                    "Discharge session: %.1f%% SOC consumed (%.0f Wh DC), "
                    "%.0f Wh AC delivered, rolling discharge efficiency %.1f%%",
                    soc_delta, consumed_wh, self._discharge_ac_wh,
                    self.discharge_efficiency_pct,
                )
        self._discharge_ac_wh = 0.0
        self._discharge_soc_start = None

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

    @property
    def roundtrip_efficiency_pct(self) -> float:
        """Assumed roundtrip efficiency used in cost calculations (always 81%)."""
        return self.ASSUMED_ROUNDTRIP_EFF * 100.0

    # ── Efficiency assumptions ──────────────────────────────────────────

    # Hardwired efficiency assumptions used in ALL cost calculations.
    # These stay fixed until we have weeks of measured data to validate.
    # Measured values are tracked and displayed but NOT used in calcs yet.
    ASSUMED_CHARGE_EFF = 0.90      # AC→DC: 90%
    ASSUMED_DISCHARGE_EFF = 0.90   # DC→AC: 90%
    ASSUMED_ROUNDTRIP_EFF = 0.81   # 90% × 90%

    @property
    def effective_cost_per_kwh(self) -> float:
        """True cost per usable kWh delivered to home via battery path.

        Uses hardwired roundtrip efficiency (81% = 90% × 90%) until
        measured values are validated over weeks of data.

        The full path:
          Grid → [charge loss 10%] → Battery → [discharge loss 10%] → Home
          1 usable AC kWh requires 1/0.81 = 1.235 AC kWh from grid
          Cost = avg_charge_price × 1.235
        """
        avg = self.avg_cost_cents_kwh
        if avg <= 0:
            return 0.0
        return avg / self.ASSUMED_ROUNDTRIP_EFF

    @property
    def measured_charge_eff(self) -> float:
        """Measured charge efficiency (informational only, not used in calcs yet)."""
        if self.charge_efficiency_pct > 0 and 50.0 <= self.charge_efficiency_pct <= 100.0:
            return self.charge_efficiency_pct
        return 0.0  # not enough data

    @property
    def measured_discharge_eff(self) -> float:
        """Measured discharge efficiency (informational only, not used in calcs yet)."""
        if self.discharge_efficiency_pct > 0 and 50.0 <= self.discharge_efficiency_pct <= 100.0:
            return self.discharge_efficiency_pct
        return 0.0  # not enough data

    @property
    def measured_roundtrip_eff(self) -> float:
        """Measured roundtrip efficiency (informational only)."""
        c = self.measured_charge_eff
        d = self.measured_discharge_eff
        if c > 0 and d > 0:
            return (c / 100.0) * (d / 100.0) * 100.0
        return 0.0

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
            # Assumed values (used in cost calculations)
            "assumed_roundtrip_pct": round(self.ASSUMED_ROUNDTRIP_EFF * 100, 0),
            "effective_cost_per_kwh": round(self.effective_cost_per_kwh, 1),
            # Measured values (informational — not used in calcs yet)
            "measured_charge_eff": round(self.measured_charge_eff, 1),
            "measured_discharge_eff": round(self.measured_discharge_eff, 1),
            "measured_roundtrip_eff": round(self.measured_roundtrip_eff, 1),
            # Raw values for backward compat
            "efficiency_pct": round(self.charge_efficiency_pct, 1),
            "charge_efficiency_pct": round(self.charge_efficiency_pct, 1),
            "discharge_efficiency_pct": round(self.discharge_efficiency_pct, 1),
            "roundtrip_efficiency_pct": round(self.roundtrip_efficiency_pct, 1),
        }

    def save_state(self) -> dict:
        """Return dict for persistence in ecoflow_state.json."""
        return {
            "legacy_wh": self.legacy_wh,
            "legacy_cost_cents": self.legacy_cost_cents,
            "observed_wh": self.observed_wh,
            "observed_cost_cents": self.observed_cost_cents,
            # Charge efficiency (AC→DC)
            "charge_total_in": self._charge_total_in,
            "charge_total_stored": self._charge_total_stored,
            "charge_efficiency_pct": self.charge_efficiency_pct,
            # Discharge efficiency (DC→AC)
            "discharge_total_out": self._discharge_total_out,
            "discharge_total_delivered": self._discharge_total_delivered,
            "discharge_efficiency_pct": self.discharge_efficiency_pct,
            # Legacy aliases (backward compat with old state files)
            "efficiency_total_in": self._charge_total_in,
            "efficiency_total_stored": self._charge_total_stored,
            "efficiency_pct": self.charge_efficiency_pct,
        }

    def load_state(self, d: dict):
        """Restore from persisted state."""
        if not d:
            return False
        self.legacy_wh = d.get("legacy_wh", 0.0)
        self.legacy_cost_cents = d.get("legacy_cost_cents", 0.0)
        self.observed_wh = d.get("observed_wh", 0.0)
        self.observed_cost_cents = d.get("observed_cost_cents", 0.0)

        # Charge efficiency — try new keys first, fall back to legacy
        self._charge_total_in = d.get("charge_total_in",
                                       d.get("efficiency_total_in", 0.0))
        self._charge_total_stored = d.get("charge_total_stored",
                                           d.get("efficiency_total_stored", 0.0))
        self.charge_efficiency_pct = d.get("charge_efficiency_pct",
                                            d.get("efficiency_pct", 0.0))
        self.efficiency_pct = self.charge_efficiency_pct  # legacy alias

        # Discharge efficiency — reset if outside sane range (bad data from old thresholds)
        disch_pct = d.get("discharge_efficiency_pct", 0.0)
        if 70.0 <= disch_pct <= 100.0:
            self._discharge_total_out = d.get("discharge_total_out", 0.0)
            self._discharge_total_delivered = d.get("discharge_total_delivered", 0.0)
            self.discharge_efficiency_pct = disch_pct
        else:
            # Reset — bogus data from short sessions with old 0.5% threshold
            self._discharge_total_out = 0.0
            self._discharge_total_delivered = 0.0
            self.discharge_efficiency_pct = 0.0
            if disch_pct > 0:
                log.info("Discharge efficiency %.1f%% outside sane range, resetting to default", disch_pct)

        log.info(
            "Battery pool loaded: legacy %.0f Wh + observed %.0f Wh = %.0f Wh, "
            "avg %.1f¢/kWh, charge eff %.1f%%, discharge eff %.1f%%, "
            "roundtrip %.1f%%, effective cost %.1f¢/kWh",
            self.legacy_wh, self.observed_wh, self.total_wh,
            self.avg_cost_cents_kwh, self.charge_efficiency_pct,
            self.discharge_efficiency_pct, self.roundtrip_efficiency_pct,
            self.effective_cost_per_kwh,
        )
        return True
