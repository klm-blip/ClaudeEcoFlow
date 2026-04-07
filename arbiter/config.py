"""Arbiter configuration — all tunable parameters in one place."""

import os

# Dashboard API
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:5000")

# Polling interval (seconds) — how often we read state + make decisions
POLL_INTERVAL = int(os.environ.get("ARBITER_POLL_INTERVAL", "30"))

# Dry-run mode: log decisions but don't send commands
DRY_RUN = os.environ.get("ARBITER_DRY_RUN", "true").lower() in ("true", "1", "yes")

# T&D rate (cents/kWh) — ComEd transmission & distribution
TD_RATE = float(os.environ.get("TD_RATE", "8.5"))

# ── Layer 1: Profitability Gate ────────────────────────────────────────────

# Never charge above this energy-only price (cents) — fallback if dashboard thresholds missing
MAX_CHARGE_ENERGY_PRICE = float(os.environ.get("MAX_CHARGE_ENERGY_PRICE", "4.0"))

# Base safety margin above effective battery cost before discharging (cents)
# This is the base_margin in the willingness formula
SAFETY_MARGIN_CENTS = float(os.environ.get("SAFETY_MARGIN_CENTS", "1.5"))

# Absolute floor — never discharge below this energy price regardless (cents)
MIN_DISCHARGE_ENERGY_PRICE = float(os.environ.get("MIN_DISCHARGE_ENERGY_PRICE", "3.0"))

# Spike override — always discharge above this energy-only price (cents)
# 15¢ energy + 8.5¢ T&D = 23.5¢ total
SPIKE_ENERGY_PRICE = float(os.environ.get("SPIKE_ENERGY_PRICE", "15.0"))

# Conservative efficiency defaults (used until measured values accumulate)
DEFAULT_CHARGE_EFFICIENCY = float(os.environ.get("DEFAULT_CHARGE_EFFICIENCY", "0.90"))
DEFAULT_DISCHARGE_EFFICIENCY = float(os.environ.get("DEFAULT_DISCHARGE_EFFICIENCY", "0.90"))

# Minimum SOC to allow discharge (safety floor — use outage_reserve from dashboard if available)
MIN_DISCHARGE_SOC = float(os.environ.get("MIN_DISCHARGE_SOC", "10.0"))

# ── Discharge Willingness — SOC penalties ──────────────────────────────────
# These are defaults; overridden by dashboard thresholds if available.
# Format: (soc_floor, penalty_cents)
# SOC >= 80%: +0¢, 60-80%: +1¢, 40-60%: +3¢, <40%: +8¢
WILLINGNESS_SOC_BANDS = [
    (80, 0.0),   # SOC >= 80%: no penalty
    (60, 1.0),   # SOC 60-80%: +1¢
    (40, 3.0),   # SOC 40-60%: +3¢
    (0,  8.0),   # SOC < 40%:  +8¢ (basically never unless spike)
]

# ── Discharge Willingness — Time-of-day adjustments ────────────────────────
# Positive = require MORE spread (less willing), Negative = require LESS (more willing)
TIMING_EVENING_PEAK = float(os.environ.get("TIMING_EVENING_PEAK", "-1.0"))       # 5 PM - midnight
TIMING_MORNING_PEAK = float(os.environ.get("TIMING_MORNING_PEAK", "-1.0"))       # 5-8 AM weekdays
TIMING_OVERNIGHT_CHEAP = float(os.environ.get("TIMING_OVERNIGHT_CHEAP", "3.0"))  # 1-5 AM

# ── Target Rate ────────────────────────────────────────────────────────────
# User's flat-rate alternative (energy only, cents). Goal: stay below this.
TARGET_ENERGY_RATE = float(os.environ.get("TARGET_ENERGY_RATE", "9.5"))

# ── 5-CP Capacity Protection ───────────────────────────────────────────────
# Defends ComEd capacity charges (PLC) on PJM 5-CP days. Forces aggressive
# discharge during the PJM peak window on high-likelihood days.
#
# Mode: "auto" (default) auto-enables on/after CP_AUTO_ENABLE_DATE each year
#       and disables before. Explicit "true"/"false" override the schedule.
CP_PROTECTION_MODE = os.environ.get("ENABLE_5CP_PROTECTION", "auto").lower()
# MM-DD on/after which auto-mode flips on each year. Off again Oct 1.
CP_AUTO_ENABLE_DATE = os.environ.get("CP_AUTO_ENABLE_DATE", "06-01")
# Hours (local ET) considered the PJM coincident peak window.
# Default 12-20 is wider than the typical 14-18 to cover edge cases —
# capacity charges dwarf any extra cycle losses on defense days.
CP_PEAK_HOUR_START = int(os.environ.get("CP_PEAK_HOUR_START", "12"))
CP_PEAK_HOUR_END   = int(os.environ.get("CP_PEAK_HOUR_END",   "20"))   # inclusive
# Score thresholds (matches capacity.py tier classification)
CP_SCORE_HIGH    = float(os.environ.get("CP_SCORE_HIGH",   "70"))
CP_SCORE_MEDIUM  = float(os.environ.get("CP_SCORE_MEDIUM", "50"))
# Months when 5-CP scoring is active (PJM peaks always Jun-Sep).
# Override via CP_ACTIVE_MONTHS="4,5,6,7,8,9" for testing.
_cp_months_env = os.environ.get("CP_ACTIVE_MONTHS")
if _cp_months_env:
    CP_ACTIVE_MONTHS = tuple(int(m) for m in _cp_months_env.split(","))
else:
    CP_ACTIVE_MONTHS = (6, 7, 8, 9)


def is_5cp_protection_enabled(today=None) -> bool:
    """Resolve 5-CP protection state — explicit override or date-based auto.

    auto: ON if today is between CP_AUTO_ENABLE_DATE and Sep 30 inclusive.
    """
    import datetime
    if CP_PROTECTION_MODE in ("true", "1", "yes", "on"):
        return True
    if CP_PROTECTION_MODE in ("false", "0", "no", "off"):
        return False
    # auto mode
    today = today or datetime.date.today()
    try:
        m, d = (int(x) for x in CP_AUTO_ENABLE_DATE.split("-"))
    except Exception:
        m, d = 6, 1
    start = datetime.date(today.year, m, d)
    end = datetime.date(today.year, 9, 30)
    return start <= today <= end

# Logging
LOG_FILE = os.environ.get("ARBITER_LOG_FILE", "logs/arbiter.csv")
