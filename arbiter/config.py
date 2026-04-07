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

# Logging
LOG_FILE = os.environ.get("ARBITER_LOG_FILE", "logs/arbiter.csv")
