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
            if not entries:
                return

            current   = entries[0][1]
            hour_avg  = float(raw_hour[0]["price"]) if raw_hour else None
            trend, sl = price_trend(entries)
            tier, col = classify_price(current)

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
