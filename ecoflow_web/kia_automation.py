"""
Kia EV charge automation: price-tiered charging with adjustable thresholds.
"""

import logging
import time

from .state import PriceState, KiaState

log = logging.getLogger("ecoflow")


class KiaAutoController:
    """Price-tiered EV charge controller.

    Three tiers based on effective electricity price:
      Soak    (price < soak_below):    charge at AC limit 100%
      Normal  (soak <= price < normal_below): charge at AC limit 90% (default)
      Expensive (price >= normal_below):       stop charging
    """

    MIN_HOLD = 60       # seconds between Kia commands (API is slower)

    def __init__(self):
        self.enabled = False
        self.last_action = None        # "charge" or "stop"
        self.last_ac_limit = None
        self.last_cmd_ts = 0.0
        self.last_decision = "\u2014"
        self.manual_override_until = 0.0

    def manual_override(self, minutes: int = 15):
        self.manual_override_until = time.time() + minutes * 60
        log.info("Kia: manual override for %d minutes", minutes)

    def cancel_override(self):
        self.manual_override_until = 0.0
        log.info("Kia: manual override cancelled")

    def decide(self, ps: PriceState, ks: KiaState, thresholds):
        """Returns (action, ac_limit, reason).

        action: "charge", "stop", or None (no change needed)
        ac_limit: target AC charge limit percent (only meaningful with "charge")
        """
        # Need price data
        if ps.effective_price is None and ps.price_hour is None and ps.price_5min is None:
            return None, None, "waiting for price data"

        ep = ps.effective_price or ps.price_hour or ps.price_5min

        # Not plugged in — nothing to do
        if not ks.plugged_in:
            return None, None, f"EV not plugged in ({ep:.1f}\u00a2)"

        # SOC at or above AC limit — already full enough
        soak_limit = getattr(thresholds, "kia_ac_limit_soak", 100)
        normal_limit = getattr(thresholds, "kia_ac_limit_normal", 90)
        soak_below = getattr(thresholds, "kia_soak_below", 1.0)
        normal_below = getattr(thresholds, "kia_normal_below", 6.0)

        if ks.soc_pct is not None:
            # Determine which limit applies at current price
            if ep < soak_below:
                effective_limit = soak_limit
            elif ep < normal_below:
                effective_limit = normal_limit
            else:
                effective_limit = 0  # expensive — don't charge

            if effective_limit > 0 and ks.soc_pct >= effective_limit:
                return None, None, f"EV at {ks.soc_pct:.0f}% (limit {effective_limit}%) \u2014 full"

        # Price tiers
        if ep < soak_below:
            return "charge", soak_limit, (
                f"SOAK: {ep:.1f}\u00a2 < {soak_below:.1f}\u00a2 \u2014 charge to {soak_limit}%"
            )

        if ep < normal_below:
            return "charge", normal_limit, (
                f"NORMAL: {ep:.1f}\u00a2 < {normal_below:.1f}\u00a2 \u2014 charge to {normal_limit}%"
            )

        return "stop", None, (
            f"EXPENSIVE: {ep:.1f}\u00a2 >= {normal_below:.1f}\u00a2 \u2014 stop charging"
        )

    def should_send(self, action, ac_limit):
        """Check if we should actually send a command. Returns (ok, reason)."""
        if not self.enabled:
            return False, "kia auto off"

        now = time.time()
        if now < self.manual_override_until:
            remaining = int(self.manual_override_until - now)
            mins, secs = divmod(remaining, 60)
            return False, f"manual override ({mins}m{secs:02d}s)"

        # No change?
        if action == self.last_action:
            if action == "charge" and ac_limit == self.last_ac_limit:
                return False, "no change"
            if action != "charge":
                return False, "no change"

        # Rate limiting
        if self.last_cmd_ts > 0:
            elapsed = now - self.last_cmd_ts
            if elapsed < self.MIN_HOLD:
                return False, f"hold {self.MIN_HOLD - elapsed:.0f}s"

        return True, "ok"

    def record(self, action, ac_limit, reason):
        self.last_action = action
        if ac_limit is not None:
            self.last_ac_limit = ac_limit
        self.last_cmd_ts = time.time()
        self.last_decision = reason
