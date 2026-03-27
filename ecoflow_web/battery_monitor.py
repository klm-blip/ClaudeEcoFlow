"""
Battery efficiency monitor — accumulates energy in/out/drain at tick level
and logs daily summaries + session details to CSV.

Key insight: integer SOC % has ~500 Wh quantization noise per tick, but over
days/weeks, total energy in vs total energy out converges to true efficiency.

Metrics tracked:
  - charge_ac_wh:    AC Wh consumed from grid during charging (battery_w > 50W)
  - discharge_ac_wh: AC Wh delivered to home during discharging (|battery_w| when < -50W)
  - vampire_wh:      Wh consumed during idle (est. ~60W constant)
  - soc_start/end:   SOC at start and end of each day
  - session details:  per charge/discharge session with timestamps, rates, SOC change
"""

import csv
import datetime
import logging
import os
import time

log = logging.getLogger("ecoflow")

BATTERY_CAPACITY_WH = 49_152  # 8 × 6,144 Wh
IDLE_THRESHOLD_W = 50         # below this, battery is idle
VAMPIRE_ESTIMATE_W = 60       # constant draw when idle


class BatteryMonitor:

    def __init__(self, log_dir: str = "logs"):
        self._log_dir = log_dir
        self._last_ts = 0.0

        # ── Running totals (persist across restarts) ──────────────
        self.charge_ac_wh = 0.0       # total AC Wh into battery
        self.discharge_ac_wh = 0.0    # total AC Wh out of battery
        self.vampire_wh = 0.0         # estimated idle drain
        self.total_seconds = 0.0      # total monitoring time

        # ── Daily totals (reset at midnight) ──────────────────────
        self._today = None
        self._day_charge_wh = 0.0
        self._day_discharge_wh = 0.0
        self._day_vampire_wh = 0.0
        self._day_soc_start = None
        self._day_soc_last = None
        self._day_seconds = 0.0

        # ── Session tracking ──────────────────────────────────────
        self._session_type = None     # "charge", "discharge", or None
        self._session_start = None    # timestamp
        self._session_wh = 0.0       # AC Wh accumulated in session
        self._session_soc_start = None
        self._session_peak_w = 0.0   # peak power in session

    def update(self, battery_w: float, soc_pct: float):
        """Called every telemetry tick (~5s). Accumulates energy."""
        now = time.time()
        if self._last_ts <= 0:
            self._last_ts = now
            self._init_day(soc_pct)
            return
        dt = now - self._last_ts
        if dt <= 0 or dt > 60:  # skip bogus gaps
            self._last_ts = now
            return
        self._last_ts = now

        # Check for day rollover
        today = datetime.date.today()
        if self._today is not None and today != self._today:
            self._flush_day(soc_pct)
            self._init_day(soc_pct)

        self._day_soc_last = soc_pct
        self.total_seconds += dt
        self._day_seconds += dt

        if battery_w > IDLE_THRESHOLD_W:
            # ── Charging ──────────────────────────────────────────
            wh = battery_w * dt / 3600.0
            self.charge_ac_wh += wh
            self._day_charge_wh += wh

            if self._session_type != "charge":
                self._end_session(soc_pct)
                self._session_type = "charge"
                self._session_start = now
                self._session_wh = 0.0
                self._session_soc_start = soc_pct
                self._session_peak_w = 0.0
            self._session_wh += wh
            self._session_peak_w = max(self._session_peak_w, battery_w)

        elif battery_w < -IDLE_THRESHOLD_W:
            # ── Discharging ───────────────────────────────────────
            wh = abs(battery_w) * dt / 3600.0
            self.discharge_ac_wh += wh
            self._day_discharge_wh += wh

            if self._session_type != "discharge":
                self._end_session(soc_pct)
                self._session_type = "discharge"
                self._session_start = now
                self._session_wh = 0.0
                self._session_soc_start = soc_pct
                self._session_peak_w = 0.0
            self._session_wh += wh
            self._session_peak_w = max(self._session_peak_w, abs(battery_w))

        else:
            # ── Idle ──────────────────────────────────────────────
            vamp = VAMPIRE_ESTIMATE_W * dt / 3600.0
            self.vampire_wh += vamp
            self._day_vampire_wh += vamp

            if self._session_type is not None:
                self._end_session(soc_pct)

    def _init_day(self, soc_pct: float):
        self._today = datetime.date.today()
        self._day_charge_wh = 0.0
        self._day_discharge_wh = 0.0
        self._day_vampire_wh = 0.0
        self._day_soc_start = soc_pct
        self._day_soc_last = soc_pct
        self._day_seconds = 0.0

    def _flush_day(self, ending_soc: float):
        """Write daily summary row to CSV."""
        if self._today is None or self._day_seconds < 60:
            return

        path = os.path.join(self._log_dir, "battery_monitor_daily.csv")
        exists = os.path.exists(path)

        soc_start = self._day_soc_start if self._day_soc_start is not None else 0
        soc_end = self._day_soc_last if self._day_soc_last is not None else ending_soc
        soc_delta = soc_end - soc_start
        stored_delta_wh = (soc_delta / 100.0) * BATTERY_CAPACITY_WH

        # Energy balance: charge - discharge - vampire ≈ stored_delta
        # Difference = losses
        balance_wh = self._day_charge_wh - self._day_discharge_wh - self._day_vampire_wh
        implied_loss_wh = balance_wh - stored_delta_wh

        # Aggregate efficiency: discharge / (discharge + losses)
        # Or simpler: discharge / charge (if SOC roughly unchanged)
        day_rt_eff = 0.0
        if self._day_charge_wh > 100:
            day_rt_eff = self._day_discharge_wh / self._day_charge_wh * 100

        try:
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if not exists:
                    w.writerow([
                        "date", "charge_wh", "discharge_wh", "vampire_wh",
                        "soc_start", "soc_end", "soc_delta",
                        "stored_delta_wh", "implied_loss_wh",
                        "day_rt_eff_pct", "hours_monitored",
                    ])
                w.writerow([
                    self._today.isoformat(),
                    round(self._day_charge_wh, 1),
                    round(self._day_discharge_wh, 1),
                    round(self._day_vampire_wh, 1),
                    round(soc_start, 1),
                    round(soc_end, 1),
                    round(soc_delta, 1),
                    round(stored_delta_wh, 1),
                    round(implied_loss_wh, 1),
                    round(day_rt_eff, 1),
                    round(self._day_seconds / 3600, 1),
                ])
            log.info(
                "Battery monitor daily: %s charge=%.0fWh discharge=%.0fWh "
                "vampire=%.0fWh SOC %d%%→%d%% loss=%.0fWh eff=%.1f%%",
                self._today, self._day_charge_wh, self._day_discharge_wh,
                self._day_vampire_wh, soc_start, soc_end,
                implied_loss_wh, day_rt_eff,
            )
        except Exception as e:
            log.error("Battery monitor: failed to write daily CSV: %s", e)

    def _end_session(self, soc_pct: float):
        """Log completed charge/discharge session to CSV."""
        if self._session_type is None or self._session_start is None:
            self._session_type = None
            return

        duration_s = time.time() - self._session_start
        if duration_s < 60 or self._session_wh < 50:
            # Too short/small to log
            self._session_type = None
            return

        soc_start = self._session_soc_start or 0
        soc_delta = soc_pct - soc_start  # positive for charge, negative for discharge
        dc_wh = abs(soc_delta) / 100.0 * BATTERY_CAPACITY_WH

        # Session efficiency
        eff = 0.0
        if self._session_type == "charge" and self._session_wh > 100:
            # AC→DC: stored / metered
            eff = dc_wh / self._session_wh * 100 if self._session_wh > 0 else 0
        elif self._session_type == "discharge" and dc_wh > 100:
            # DC→AC: delivered / consumed
            eff = self._session_wh / dc_wh * 100 if dc_wh > 0 else 0

        path = os.path.join(self._log_dir, "battery_monitor_sessions.csv")
        exists = os.path.exists(path)

        try:
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if not exists:
                    w.writerow([
                        "timestamp", "type", "duration_min", "ac_wh",
                        "soc_start", "soc_end", "soc_delta",
                        "dc_wh_est", "peak_w", "efficiency_pct",
                    ])
                w.writerow([
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    self._session_type,
                    round(duration_s / 60, 1),
                    round(self._session_wh, 1),
                    round(soc_start, 1),
                    round(soc_pct, 1),
                    round(soc_delta, 1),
                    round(dc_wh, 1),
                    round(self._session_peak_w, 0),
                    round(eff, 1),
                ])
        except Exception as e:
            log.error("Battery monitor: failed to write session CSV: %s", e)

        self._session_type = None

    # ── Aggregate stats ───────────────────────────────────────────────

    @property
    def aggregate_roundtrip_pct(self) -> float:
        """Long-term roundtrip efficiency: discharge / charge."""
        if self.charge_ac_wh < 1000:  # need at least 1 kWh
            return 0.0
        return self.discharge_ac_wh / self.charge_ac_wh * 100

    @property
    def monitoring_hours(self) -> float:
        return self.total_seconds / 3600

    def to_dict(self) -> dict:
        return {
            "charge_kwh": round(self.charge_ac_wh / 1000, 2),
            "discharge_kwh": round(self.discharge_ac_wh / 1000, 2),
            "vampire_kwh": round(self.vampire_wh / 1000, 2),
            "aggregate_roundtrip_pct": round(self.aggregate_roundtrip_pct, 1),
            "monitoring_hours": round(self.monitoring_hours, 1),
            "monitoring_days": round(self.monitoring_hours / 24, 1),
        }

    # ── Persistence ───────────────────────────────────────────────────

    def save_state(self) -> dict:
        return {
            "charge_ac_wh": self.charge_ac_wh,
            "discharge_ac_wh": self.discharge_ac_wh,
            "vampire_wh": self.vampire_wh,
            "total_seconds": self.total_seconds,
        }

    def load_state(self, d: dict):
        if not d:
            return
        self.charge_ac_wh = d.get("charge_ac_wh", 0.0)
        self.discharge_ac_wh = d.get("discharge_ac_wh", 0.0)
        self.vampire_wh = d.get("vampire_wh", 0.0)
        self.total_seconds = d.get("total_seconds", 0.0)
        log.info(
            "Battery monitor loaded: charge=%.1fkWh discharge=%.1fkWh "
            "vampire=%.1fkWh rt_eff=%.1f%% over %.1f days",
            self.charge_ac_wh / 1000, self.discharge_ac_wh / 1000,
            self.vampire_wh / 1000, self.aggregate_roundtrip_pct,
            self.monitoring_hours / 24,
        )
