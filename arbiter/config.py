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

# Never charge above this energy-only price (cents)
MAX_CHARGE_ENERGY_PRICE = float(os.environ.get("MAX_CHARGE_ENERGY_PRICE", "4.0"))

# Safety margin above effective battery cost before discharging (cents)
SAFETY_MARGIN_CENTS = float(os.environ.get("SAFETY_MARGIN_CENTS", "3.0"))

# Absolute floor — never discharge below this energy price regardless (cents)
MIN_DISCHARGE_ENERGY_PRICE = float(os.environ.get("MIN_DISCHARGE_ENERGY_PRICE", "3.0"))

# Spike override — always discharge above this TOTAL price (energy + T&D) (cents)
SPIKE_OVERRIDE_TOTAL_PRICE = float(os.environ.get("SPIKE_OVERRIDE_TOTAL_PRICE", "23.5"))

# Conservative efficiency defaults (used until measured values accumulate)
DEFAULT_CHARGE_EFFICIENCY = float(os.environ.get("DEFAULT_CHARGE_EFFICIENCY", "0.90"))
DEFAULT_DISCHARGE_EFFICIENCY = float(os.environ.get("DEFAULT_DISCHARGE_EFFICIENCY", "0.90"))

# Minimum SOC to allow discharge (safety floor)
MIN_DISCHARGE_SOC = float(os.environ.get("MIN_DISCHARGE_SOC", "10.0"))

# Logging
LOG_FILE = os.environ.get("ARBITER_LOG_FILE", "logs/arbiter.csv")
