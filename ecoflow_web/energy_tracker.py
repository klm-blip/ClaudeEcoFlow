"""
Hourly energy use and cost tracker.

Accumulates grid/load/battery energy per hour, flushes completed hours
to daily CSV files for historical review.
"""

import csv
import datetime
import logging
import os
import time

log = logging.getLogger("ecoflow")

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG_DIR = os.path.join(_PROJECT_DIR, "logs")

_ENERGY_HEADERS = [
    "hour", "grid_kwh", "load_kwh", "battery_charge_kwh",
    "battery_discharge_kwh", "cost_cents", "avg_price_cents",
]


class EnergyTracker:

    def __init__(self):
        self.current_hour = -1
        self.current_date = ""

        # Accumulators for the current hour
        self.grid_wh = 0.0
        self.load_wh = 0.0
        self.battery_charge_wh = 0.0
        self.battery_discharge_wh = 0.0
        self.cost_cents = 0.0
        self._price_sum = 0.0     # sum of effective_price samples
        self._price_count = 0     # number of samples (for avg_price)

        self._last_ts = 0.0

    # ── Core update (called every telemetry tick) ─────────────────────────

    def update(self, grid_w: float, load_w: float, battery_w: float,
               effective_price: float, energy_price: float = None):
        """Accumulate energy for the current hour. Flushes on hour rollover."""
        now = time.time()
        now_dt = datetime.datetime.now()
        cur_hour = now_dt.hour
        cur_date = now_dt.strftime("%Y-%m-%d")

        # Hour rollover detection
        if self.current_hour >= 0 and (cur_hour != self.current_hour
                                        or cur_date != self.current_date):
            self._flush_hour()

        # Initialize or reset for new hour
        if cur_hour != self.current_hour or cur_date != self.current_date:
            self.current_hour = cur_hour
            self.current_date = cur_date
            self.grid_wh = 0.0
            self.load_wh = 0.0
            self.battery_charge_wh = 0.0
            self.battery_discharge_wh = 0.0
            self.cost_cents = 0.0
            self._price_sum = 0.0
            self._price_count = 0

        # Compute dt
        if self._last_ts <= 0:
            self._last_ts = now
            return
        dt = now - self._last_ts
        if dt <= 0 or dt > 60:
            self._last_ts = now
            return
        self._last_ts = now

        # Accumulate energy (Wh = W × seconds / 3600)
        dt_h = dt / 3600.0

        grid = max(0.0, grid_w or 0.0)
        load = max(0.0, load_w or 0.0)

        self.grid_wh += grid * dt_h
        self.load_wh += load * dt_h

        bw = battery_w or 0.0
        if bw > 50:
            self.battery_charge_wh += bw * dt_h
        elif bw < -50:
            self.battery_discharge_wh += abs(bw) * dt_h

        # Cost = grid energy only (battery discharge is free)
        if effective_price is not None and grid > 0:
            self.cost_cents += (grid * dt_h / 1000.0) * effective_price
            # avg_price tracks energy-only price (matches ComEd) for display;
            # cost_cents uses effective_price which includes T&D.
            display_price = energy_price if energy_price is not None else effective_price
            self._price_sum += display_price
            self._price_count += 1

    # ── Flush completed hour to CSV ───────────────────────────────────────

    def _flush_hour(self):
        """Write the completed hour to the daily energy CSV."""
        if self.current_hour < 0 or self.current_date == "":
            return

        avg_price = (self._price_sum / self._price_count
                     if self._price_count > 0 else 0.0)

        row = [
            self.current_hour,
            f"{self.grid_wh / 1000.0:.3f}",
            f"{self.load_wh / 1000.0:.3f}",
            f"{self.battery_charge_wh / 1000.0:.3f}",
            f"{self.battery_discharge_wh / 1000.0:.3f}",
            f"{self.cost_cents:.2f}",
            f"{avg_price:.2f}",
        ]

        os.makedirs(_LOG_DIR, exist_ok=True)
        path = os.path.join(_LOG_DIR, f"energy_{self.current_date}.csv")
        is_new = not os.path.exists(path)
        try:
            with open(path, "a", newline="") as f:
                w = csv.writer(f)
                if is_new:
                    w.writerow(_ENERGY_HEADERS)
                w.writerow(row)
            log.info("Energy hour %02d flushed to %s", self.current_hour, path)
        except Exception as e:
            log.warning("Failed to write energy CSV: %s", e)

    # ── Serialization ─────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Current hour running totals for WebSocket broadcast."""
        return {
            "hour": self.current_hour,
            "date": self.current_date,
            "grid_kwh": round(self.grid_wh / 1000.0, 3),
            "load_kwh": round(self.load_wh / 1000.0, 3),
            "battery_charge_kwh": round(self.battery_charge_wh / 1000.0, 3),
            "battery_discharge_kwh": round(self.battery_discharge_wh / 1000.0, 3),
            "cost_cents": round(self.cost_cents, 2),
            "avg_price_cents": round(
                self._price_sum / self._price_count
                if self._price_count > 0 else 0.0, 2
            ),
        }

    def save_state(self) -> dict:
        """Return dict for persistence in ecoflow_state.json."""
        return {
            "hour": self.current_hour,
            "date": self.current_date,
            "grid_wh": self.grid_wh,
            "load_wh": self.load_wh,
            "battery_charge_wh": self.battery_charge_wh,
            "battery_discharge_wh": self.battery_discharge_wh,
            "cost_cents": self.cost_cents,
            "price_sum": self._price_sum,
            "price_count": self._price_count,
        }

    def load_state(self, d: dict):
        """Restore from persisted state."""
        if not d:
            return
        saved_date = d.get("date", "")
        saved_hour = d.get("hour", -1)

        # Only restore if same date+hour (otherwise stale)
        now_dt = datetime.datetime.now()
        if saved_date == now_dt.strftime("%Y-%m-%d") and saved_hour == now_dt.hour:
            self.current_hour = saved_hour
            self.current_date = saved_date
            self.grid_wh = d.get("grid_wh", 0.0)
            self.load_wh = d.get("load_wh", 0.0)
            self.battery_charge_wh = d.get("battery_charge_wh", 0.0)
            self.battery_discharge_wh = d.get("battery_discharge_wh", 0.0)
            self.cost_cents = d.get("cost_cents", 0.0)
            self._price_sum = d.get("price_sum", 0.0)
            self._price_count = d.get("price_count", 0)
            log.info("Energy tracker restored: hour %d, date %s",
                     self.current_hour, self.current_date)
        else:
            log.info("Energy tracker: saved state is stale (hour %d/%s vs now %d/%s), starting fresh",
                     saved_hour, saved_date, now_dt.hour, now_dt.strftime("%Y-%m-%d"))

    def flush_partial(self):
        """Flush the current (incomplete) hour to CSV on shutdown.

        This prevents data loss when the container is rebuilt mid-hour.
        The next startup will overwrite this partial row if the hour
        hasn't changed (same date+hour row gets appended, which is fine
        since read_day returns all rows — the last one for a given hour
        will have the most accumulated data).
        """
        if self.current_hour >= 0 and self.current_date:
            self._flush_hour()
            log.info("Energy tracker: flushed partial hour %d on shutdown",
                     self.current_hour)

    # ── CSV reading for API ───────────────────────────────────────────────

    @staticmethod
    def read_day(date_str: str) -> list:
        """Read energy CSV for a given date, return list of dicts.

        Deduplicates by hour (keeps last row per hour) so that partial
        flushes on shutdown don't create duplicate entries.
        """
        path = os.path.join(_LOG_DIR, f"energy_{date_str}.csv")
        if not os.path.exists(path):
            return []
        try:
            with open(path, newline="") as f:
                rows = list(csv.DictReader(f))
            # Deduplicate: last row per hour wins (most data accumulated)
            by_hour = {}
            for r in rows:
                by_hour[r.get("hour", "")] = r
            return [by_hour[h] for h in sorted(by_hour.keys(), key=lambda x: int(x) if x.isdigit() else 0)]
        except Exception as e:
            log.warning("Failed to read energy CSV %s: %s", path, e)
            return []

    @staticmethod
    def available_dates() -> list:
        """Return sorted list of dates that have energy CSV files."""
        if not os.path.isdir(_LOG_DIR):
            return []
        dates = []
        for name in os.listdir(_LOG_DIR):
            if name.startswith("energy_") and name.endswith(".csv"):
                date_str = name[7:-4]  # strip "energy_" and ".csv"
                dates.append(date_str)
        dates.sort(reverse=True)
        return dates

    @staticmethod
    def summarize_period(start_date: str, end_date: str) -> dict:
        """Aggregate energy data across a date range."""
        totals = {
            "grid_kwh": 0.0, "load_kwh": 0.0,
            "battery_charge_kwh": 0.0, "battery_discharge_kwh": 0.0,
            "cost_cents": 0.0, "hours": 0,
        }
        try:
            d = datetime.date.fromisoformat(start_date)
            end = datetime.date.fromisoformat(end_date)
        except ValueError:
            return totals

        while d <= end:
            rows = EnergyTracker.read_day(d.isoformat())
            for r in rows:
                try:
                    totals["grid_kwh"] += float(r.get("grid_kwh", 0))
                    totals["load_kwh"] += float(r.get("load_kwh", 0))
                    totals["battery_charge_kwh"] += float(r.get("battery_charge_kwh", 0))
                    totals["battery_discharge_kwh"] += float(r.get("battery_discharge_kwh", 0))
                    totals["cost_cents"] += float(r.get("cost_cents", 0))
                    totals["hours"] += 1
                except (ValueError, TypeError):
                    pass
            d += datetime.timedelta(days=1)

        # Round
        for k in ["grid_kwh", "load_kwh", "battery_charge_kwh",
                   "battery_discharge_kwh", "cost_cents"]:
            totals[k] = round(totals[k], 3)
        if totals["grid_kwh"] > 0:
            totals["avg_price_cents"] = round(
                totals["cost_cents"] / totals["grid_kwh"], 2
            )
        else:
            totals["avg_price_cents"] = 0.0
        return totals
