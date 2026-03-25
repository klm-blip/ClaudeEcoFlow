"""
ComEd real-time pricing: 5-minute feed + hourly average polling.
"""

import datetime
import json
import logging
import threading
import time
import urllib.request

from .config import COMED_POLL_SECONDS, COMED_FIXED_RATE, COMED_5MIN_URL, COMED_HOURAVG_URL
from .state import PriceState

log = logging.getLogger("ecoflow")


def classify_price(cents: float) -> tuple:
    """Return (tier_label, hex_color) for a price in cents/kWh."""
    if cents < 0:                    return "NEGATIVE",  "#00ff88"
    elif cents < 3:                  return "VERY LOW",  "#3fb950"
    elif cents < 6:                  return "LOW",       "#58a6ff"
    elif cents < COMED_FIXED_RATE:   return "MODERATE",  "#f0a500"
    elif cents < 14:                 return "HIGH",      "#f85149"
    else:                            return "SPIKE",     "#ff4040"


def price_trend(entries: list, n=6) -> tuple:
    """
    entries: [(timestamp, price_cents), ...] newest first.
    Returns (direction_str, slope_float) — positive slope = rising over time.
    """
    if len(entries) < 2:
        return "flat", 0.0
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


def detect_trend_alert(entries: list, threshold: float = 8.0,
                       consecutive: int = 3) -> tuple:
    """Check if the last N 5-minute readings are all above threshold.

    This detects sustained price elevations mid-hour that predict the full
    hour will be expensive — before the hourly average crosses the discharge
    threshold.

    Args:
        entries: [(timestamp, price), ...] newest first
        threshold: price in cents above which a reading is "elevated"
        consecutive: how many consecutive elevated readings needed

    Returns:
        (alert_fired: bool, minute_of_hour: int or None)
    """
    if len(entries) < consecutive:
        return False, None

    # Check the N most recent readings (newest first)
    recent = entries[:consecutive]
    all_above = all(price >= threshold for _, price in recent)

    if all_above:
        # The oldest reading in the window tells us when the trend started
        oldest_ts = recent[-1][0]
        minute = datetime.datetime.fromtimestamp(oldest_ts).minute
        return True, minute

    return False, None


class ComedPoller:
    """Polls ComEd APIs in a background thread every COMED_POLL_SECONDS."""

    def __init__(self, price_state: PriceState, on_update):
        self.ps              = price_state
        self.on_update       = on_update
        self._stop           = threading.Event()
        self._prev_hour_avg  = None   # last confirmed hourly avg (for stale detection)
        self._current_hour   = -1     # hour number when _prev_hour_avg was set
        self._alert_hour     = -1     # hour when trend alert last fired (reset each hour)

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
            if not entries:
                return

            current     = entries[0][1]
            current_ts  = entries[0][0]   # unix timestamp of latest 5-min price
            hour_avg    = float(raw_hour[0]["price"]) if raw_hour else None
            trend, sl = price_trend(entries)
            tier, col = classify_price(current)

            self.ps.price_5min    = current
            self.ps.price_5min_ts = current_ts
            self.ps.price_hour    = hour_avg
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

            # Trend alert: detect consecutive elevated 5-min prices
            # Configurable via thresholds (trend_alert_threshold, trend_alert_count)
            alert_thresh = getattr(self, 'trend_alert_threshold', 8.0)
            alert_count = getattr(self, 'trend_alert_count', 3)
            alert_enabled = getattr(self, 'trend_alert_enabled', True)

            now_hour = datetime.datetime.now().hour
            if now_hour != self._alert_hour:
                # New hour — reset alert (hourly avg resets, new billing period)
                self.ps.trend_alert = False
                self.ps.trend_alert_minute = None
                self._alert_hour = now_hour

            if alert_enabled:
                fired, minute = detect_trend_alert(entries, alert_thresh, alert_count)
                if fired and not self.ps.trend_alert:
                    self.ps.trend_alert = True
                    self.ps.trend_alert_minute = minute
                    log.info("TREND ALERT: %d consecutive 5-min prices >= %.1f\u00a2 (minute %s)",
                             alert_count, alert_thresh, minute)

            # Effective price = ComEd hourly average (what BESH actually bills on).
            # At hour boundaries (minutes 0-9), the hourly API may still report
            # last hour's stale average.  Detect and use 5-min avg instead.
            now_dt = datetime.datetime.now()
            current_hour = now_dt.hour

            # On hour change, snapshot the old hourly avg for stale detection
            if self._current_hour != current_hour:
                if self.ps.price_hour is not None:
                    self._prev_hour_avg = self.ps.price_hour
                self._current_hour = current_hour

            if hour_avg is not None:
                minute = now_dt.minute
                # In first 10 min of the hour, check if hourly avg is stale
                if (minute < 10
                        and self._prev_hour_avg is not None
                        and abs(hour_avg - self._prev_hour_avg) < 0.01):
                    # Hourly avg unchanged — still reporting last hour's price.
                    # Bridge with average of last 2 five-minute prices.
                    last2 = [p for _, p in entries[:2]]
                    if len(last2) >= 2:
                        self.ps.effective_price = sum(last2) / len(last2)
                        log.info("Hour-start bridge: using avg of last 2 5min prices (%.1f¢)",
                                 self.ps.effective_price)
                    else:
                        self.ps.effective_price = current
                else:
                    # Hourly avg is fresh — use it
                    self.ps.effective_price = hour_avg
                    if minute >= 10:
                        # Past the stale window — safe to update prev for next hour
                        self._prev_hour_avg = hour_avg
            else:
                # Fallback: no hourly avg yet (rare) — use latest 5-min price
                self.ps.effective_price = current

            log.info("ComEd: 5min=%.1f\u00a2  hour=%s\u00a2  running=%s\u00a2  eff=%s\u00a2  trend=%s(%.2f)  tier=%s",
                     current,
                     f"{hour_avg:.1f}" if hour_avg else "?",
                     f"{self.ps.running_hour_avg:.1f}" if self.ps.running_hour_avg else "?",
                     f"{self.ps.effective_price:.1f}" if self.ps.effective_price else "?",
                     trend, sl, tier)
            self.on_update()

        except Exception as e:
            self.ps.error = str(e)
            log.warning("ComEd poll failed: %s", e)
