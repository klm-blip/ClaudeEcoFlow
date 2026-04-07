"""The Arbiter — autonomous energy management brain.

Runs as a standalone process. Polls the dashboard for state,
evaluates profitability, and sends commands back via HTTP API.

Usage:
    python -m arbiter.main                      # dry-run (default)
    ARBITER_DRY_RUN=false python -m arbiter.main  # live mode

Environment variables (all optional, see config.py for defaults):
    DASHBOARD_URL          http://localhost:5000
    ARBITER_POLL_INTERVAL  30 (seconds)
    ARBITER_DRY_RUN        true
    TD_RATE                8.5
    SAFETY_MARGIN_CENTS      2.0
    MIN_DISCHARGE_ENERGY_PRICE  3.0
    SPIKE_ENERGY_PRICE         15.0
    TARGET_ENERGY_RATE         9.5
"""

import csv
import datetime
import json
import logging
import os
import re
import sys
import time

import requests

from . import config
from .profitability import evaluate
from . import capacity_live

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ARBITER] %(message)s",
)
log = logging.getLogger("arbiter")


def _fetch_state() -> dict | None:
    """Get full dashboard state via HTTP."""
    try:
        resp = requests.get(f"{config.DASHBOARD_URL}/api/state", timeout=10)
        resp.raise_for_status()
        state = resp.json()
        # Debug: log key values to diagnose missing data
        power = state.get("power", {})
        price = state.get("price", {})
        log.debug("State: SOC=%s, price=%s, battery_w=%s, stale=%s",
                  power.get("soc_pct"), price.get("effective_price"),
                  power.get("battery_w"), power.get("stale"))
        return state
    except Exception as e:
        log.warning("Failed to fetch state from dashboard: %s", e)
        return None


def _send_action(action: str, reason: str, rate: int = None, max_soc: int = None):
    """Send a command to the dashboard."""
    body = {
        "action": action,
        "reason": reason,
        "dry_run": config.DRY_RUN,
    }
    if rate is not None:
        body["rate"] = rate
    if max_soc is not None:
        body["max_soc"] = max_soc

    try:
        resp = requests.post(
            f"{config.DASHBOARD_URL}/api/arbiter/action",
            json=body,
            timeout=10,
        )
        result = resp.json()
        executed = result.get("executed", False)
        tag = "EXECUTED" if executed else "DRY-RUN"
        log.info("[%s] %s -> %s", tag, action, reason)
        return result
    except Exception as e:
        log.warning("Failed to send action to dashboard: %s", e)
        return None


def _extract_charge_rate(reason: str) -> int | None:
    """Extract charge rate from reason string like '... charge at 3000W'."""
    m = re.search(r"(\d+)W", reason)
    return int(m.group(1)) if m else None


def _log_csv(state: dict, action: str, reason: str):
    """Append decision to CSV log."""
    os.makedirs(os.path.dirname(config.LOG_FILE) or ".", exist_ok=True)
    now = datetime.datetime.now()
    file_exists = os.path.exists(config.LOG_FILE)

    price = state.get("price", {})
    power = state.get("power", {})
    battery = state.get("battery_cost", {})
    thresholds = state.get("thresholds", {})

    row = {
        "timestamp": now.isoformat(timespec="seconds"),
        "energy_price": price.get("effective_price", ""),
        "total_grid_cost": round((price.get("effective_price", 0) or 0) + config.TD_RATE, 1),
        "soc_pct": power.get("soc_pct", ""),
        "battery_avg_cost": battery.get("avg_cost_cents_kwh", ""),
        "effective_battery_cost": battery.get("effective_cost_per_kwh", ""),
        "outage_reserve": thresholds.get("outage_reserve_pct", 20),
        "action": action,
        "reason": reason,
        "dry_run": config.DRY_RUN,
    }

    with open(config.LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


# ── State tracking to avoid spamming same command ─────────────────────────

_last_action = None
_last_action_ts = 0.0
_MIN_REPEAT_INTERVAL = 120  # don't repeat same action within 2 minutes


def _should_send(action: str) -> bool:
    """Avoid sending the same action repeatedly."""
    global _last_action, _last_action_ts

    if action == "hold":
        return True  # hold is always fine to log (no command sent)

    if action == _last_action and (time.time() - _last_action_ts) < _MIN_REPEAT_INTERVAL:
        return False  # same action too recently

    return True


def _record_action(action: str):
    """Record that we sent an action."""
    global _last_action, _last_action_ts
    if action != "hold":
        _last_action = action
        _last_action_ts = time.time()


# ── Main loop ──────────────────────────────────────────────────────────────

def run():
    mode_str = "DRY-RUN" if config.DRY_RUN else "LIVE"
    log.info("=" * 60)
    log.info("The Arbiter starting [%s]", mode_str)
    log.info("Dashboard: %s", config.DASHBOARD_URL)
    log.info("Poll interval: %ds", config.POLL_INTERVAL)
    log.info("Base margin: %.1f¢ | Discharge floor: %.1f¢ energy",
             config.SAFETY_MARGIN_CENTS, config.MIN_DISCHARGE_ENERGY_PRICE)
    log.info("Spike override: %.1f¢ energy | Target rate: %.1f¢ energy",
             config.SPIKE_ENERGY_PRICE, config.TARGET_ENERGY_RATE)
    log.info("T&D rate: %.1f¢", config.TD_RATE)
    log.info("SOC willingness bands: %s", config.WILLINGNESS_SOC_BANDS)
    log.info("5-CP protection: %s [mode=%s, auto-enable=%s] (peak %d-%d ET, HIGH>=%.0f, MEDIUM>=%.0f)",
             "ON" if config.is_5cp_protection_enabled() else "OFF",
             config.CP_PROTECTION_MODE, config.CP_AUTO_ENABLE_DATE,
             config.CP_PEAK_HOUR_START, config.CP_PEAK_HOUR_END,
             config.CP_SCORE_HIGH, config.CP_SCORE_MEDIUM)
    log.info("Timing: evening %.1f¢, morning %.1f¢, overnight %+.1f¢",
             config.TIMING_EVENING_PEAK, config.TIMING_MORNING_PEAK,
             config.TIMING_OVERNIGHT_CHEAP)
    log.info("=" * 60)

    while True:
        try:
            state = _fetch_state()
            if state is None:
                log.warning("No state — dashboard unreachable, will retry in %ds", config.POLL_INTERVAL)
                time.sleep(config.POLL_INTERVAL)
                continue

            # Inject 5-CP capacity score (cached daily inside capacity_live)
            if config.is_5cp_protection_enabled():
                try:
                    cp = capacity_live.get_today_score()
                    if cp is not None:
                        state["capacity_score"] = cp.score
                        state["capacity_tier"] = cp.tier
                        state["capacity_in_peak_window"] = capacity_live.in_peak_window()
                except Exception as e:
                    log.warning("capacity scoring failed: %s", e)

            action, reason = evaluate(state)

            if _should_send(action):
                # Extract charge rate from reason if charging
                rate = _extract_charge_rate(reason) if action == "charge" else None

                _send_action(action, reason, rate=rate)
                _record_action(action)
            else:
                log.debug("Suppressed repeat: %s", action)

            _log_csv(state, action, reason)

        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception:
            log.exception("Unexpected error in main loop")

        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    run()
