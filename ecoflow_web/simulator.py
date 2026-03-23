"""
Alternate-world simulator: replays a day's energy data through the Arbiter's
decision logic, tracking simulated battery SOC and cost.

Compares actual (manual) cost vs what the Arbiter would have achieved.
"""

import csv
import datetime
import logging
import os
import sys

# Add project root to path so we can import arbiter
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

from arbiter.profitability import evaluate
from arbiter import config as arb_config

log = logging.getLogger("ecoflow")

# Battery parameters
BATTERY_CAPACITY_KWH = 49.152  # 8 × 6.144 kWh
CHARGE_EFFICIENCY = 0.90
DISCHARGE_EFFICIENCY = 0.90
ROUNDTRIP_EFFICIENCY = CHARGE_EFFICIENCY * DISCHARGE_EFFICIENCY  # 0.81
VAMPIRE_DRAIN_KW = 0.060  # ~60W constant draw

# Charge rates by band (kW) — match dashboard defaults
DEFAULT_CHARGE_RATES = {
    "HIGH": 1.5,
    "MID": 3.0,
    "LOW": 6.0,
    "EMERGENCY": 6.0,
}


def simulate_day(date_str: str, energy_rows: list, thresholds: dict,
                 starting_soc: float = None,
                 battery_avg_cost: float = 10.5,
                 actual_ending_soc: float = None) -> dict:
    """Run the Arbiter's logic against a day's actual energy data.

    Args:
        date_str: Date being simulated (YYYY-MM-DD)
        energy_rows: List of hourly dicts from EnergyTracker.read_day()
        thresholds: Dashboard thresholds (charge bands, outage reserve, etc.)
        starting_soc: SOC at start of day (%). If None, estimates from data.
        battery_avg_cost: Average cost of energy in battery (cents/kWh)
        actual_ending_soc: Real system SOC right now — used to compute refill cost

    Returns:
        Dict with hourly comparison and totals.
    """
    if not energy_rows:
        return {"date": date_str, "hours": [], "totals": {}}

    # ── Determine starting SOC ──────────────────────────────────────────
    if starting_soc is None:
        # Try to read from arbiter CSV for midnight
        starting_soc = _estimate_starting_soc(date_str)
    if starting_soc is None:
        starting_soc = 80.0  # reasonable default

    sim_soc = starting_soc
    # Use battery avg cost, but never 0 — fall back to 10.5¢ (legacy estimate)
    sim_battery_cost_pool = battery_avg_cost if battery_avg_cost > 0 else 10.5

    hours = []
    total_manual_cost = 0.0
    total_arbiter_cost = 0.0
    total_manual_grid_kwh = 0.0
    total_arbiter_grid_kwh = 0.0

    for row in energy_rows:
        try:
            hour = int(row.get("hour", 0))
            grid_kwh = float(row.get("grid_kwh", 0))
            load_kwh = float(row.get("load_kwh", 0))
            charge_kwh = float(row.get("battery_charge_kwh", 0))
            discharge_kwh = float(row.get("battery_discharge_kwh", 0))
            actual_cost = float(row.get("cost_cents", 0))
            avg_price = float(row.get("avg_price_cents", 0))
        except (ValueError, TypeError):
            continue

        # ── Manual (actual) side ────────────────────────────────────────
        # actual_cost from CSV = grid energy cost only (charge cost already included)
        # Discharge is "free" — energy was paid for when charged
        total_manual_cost += actual_cost
        total_manual_grid_kwh += grid_kwh

        # ── Build a synthetic state dict for the Arbiter ────────────────
        # The Arbiter's evaluate() expects the full dashboard state
        effective_cost = sim_battery_cost_pool / ROUNDTRIP_EFFICIENCY if sim_battery_cost_pool > 0 else 0

        state = {
            "price": {
                "effective_price": avg_price,
            },
            "power": {
                "soc_pct": sim_soc,
                "battery_w": 0,
                "stale": False,
            },
            "battery_cost": {
                "avg_cost_cents_kwh": sim_battery_cost_pool,
                "effective_cost_per_kwh": effective_cost,
            },
            "thresholds": thresholds,
        }

        # ── Ask the Arbiter what it would do ────────────────────────────
        action, reason = evaluate(state)

        # ── Simulate the Arbiter's action ───────────────────────────────
        total_cost_per_kwh = avg_price + arb_config.TD_RATE
        sim_hour_cost = 0.0
        sim_hour_grid_kwh = 0.0
        sim_charge_kwh = 0.0
        sim_discharge_kwh = 0.0
        soc_before = sim_soc

        if action == "discharge" and sim_soc > thresholds.get("outage_reserve_pct", 20):
            # Battery covers the home load
            # How much SOC does load_kwh consume? (accounting for discharge efficiency)
            kwh_from_battery = load_kwh / DISCHARGE_EFFICIENCY
            soc_delta = (kwh_from_battery / BATTERY_CAPACITY_KWH) * 100

            available_soc = sim_soc - thresholds.get("outage_reserve_pct", 20)
            if soc_delta > available_soc:
                # Can't cover full hour — partial discharge, rest from grid
                actual_discharge_soc = available_soc
                actual_discharge_kwh = (actual_discharge_soc / 100) * BATTERY_CAPACITY_KWH * DISCHARGE_EFFICIENCY
                remaining_load = load_kwh - actual_discharge_kwh
                sim_soc -= actual_discharge_soc
                sim_hour_grid_kwh = remaining_load
                sim_hour_cost = remaining_load * total_cost_per_kwh
                sim_discharge_kwh = actual_discharge_kwh
            else:
                # Battery covers full load — no grid cost (energy already paid for when charged)
                sim_soc -= soc_delta
                sim_hour_grid_kwh = 0
                sim_hour_cost = 0
                sim_discharge_kwh = load_kwh

        elif action == "charge":
            # Grid covers home load + charges battery
            # Determine charge rate from band
            from arbiter.profitability import _get_charge_band
            band_name, _, band_rate = _get_charge_band(sim_soc, thresholds)
            charge_rate_kw = band_rate / 1000.0
            # Max charge in 1 hour at this rate
            max_charge_kwh = charge_rate_kw
            # How much SOC room?
            max_soc = thresholds.get("max_soc", 95)
            room_pct = max_soc - sim_soc
            room_kwh = (room_pct / 100) * BATTERY_CAPACITY_KWH

            actual_charge_kwh = min(max_charge_kwh, room_kwh)
            # Grid power needed = home load + charge (accounting for charge efficiency)
            grid_for_charge = actual_charge_kwh / CHARGE_EFFICIENCY
            sim_hour_grid_kwh = load_kwh + grid_for_charge
            sim_hour_cost = sim_hour_grid_kwh * total_cost_per_kwh

            # Update simulated SOC
            soc_gain = (actual_charge_kwh / BATTERY_CAPACITY_KWH) * 100
            sim_soc = min(max_soc, sim_soc + soc_gain)
            sim_charge_kwh = actual_charge_kwh

            # Update battery cost pool (weighted average)
            charge_cost_per_kwh = total_cost_per_kwh / CHARGE_EFFICIENCY
            old_kwh = (soc_before / 100) * BATTERY_CAPACITY_KWH
            if old_kwh + actual_charge_kwh > 0:
                sim_battery_cost_pool = (
                    (old_kwh * sim_battery_cost_pool + actual_charge_kwh * total_cost_per_kwh)
                    / (old_kwh + actual_charge_kwh)
                )

        else:
            # hold / backup — grid covers home load, battery untouched
            sim_hour_grid_kwh = load_kwh
            sim_hour_cost = load_kwh * total_cost_per_kwh

        # Apply vampire drain (constant regardless of action)
        vampire_kwh = VAMPIRE_DRAIN_KW  # per hour
        vampire_soc = (vampire_kwh / BATTERY_CAPACITY_KWH) * 100
        sim_soc = max(0, sim_soc - vampire_soc)

        total_arbiter_cost += sim_hour_cost
        total_arbiter_grid_kwh += sim_hour_grid_kwh

        hours.append({
            "hour": hour,
            "load_kwh": round(load_kwh, 3),
            "avg_price": round(avg_price, 2),
            "total_price": round(total_cost_per_kwh, 2),
            # Manual (actual)
            "manual_grid_kwh": round(grid_kwh, 3),
            "manual_cost": round(actual_cost, 2),
            "manual_charge_kwh": round(charge_kwh, 3),
            "manual_discharge_kwh": round(discharge_kwh, 3),
            # Arbiter simulation
            "arbiter_action": action,
            "arbiter_reason": reason,
            "arbiter_grid_kwh": round(sim_hour_grid_kwh, 3),
            "arbiter_cost": round(sim_hour_cost, 2),
            "arbiter_charge_kwh": round(sim_charge_kwh, 3),
            "arbiter_discharge_kwh": round(sim_discharge_kwh, 3),
            "arbiter_soc": round(sim_soc, 1),
        })

    # ── Refill cost: bring simulated battery to actual system SOC ──────
    # If the Arbiter used more battery than manual, it needs to refill
    # more — that future grid cost should be counted.
    # If the Arbiter used less (held battery), it would need less refill.
    # Use actual ending SOC as the target (apples-to-apples).
    refill_cost = 0.0
    refill_kwh = 0.0
    soc_gap = 0.0
    if actual_ending_soc is not None:
        soc_gap = actual_ending_soc - sim_soc  # positive = Arbiter needs to buy more
        if soc_gap > 0:
            # Arbiter's battery is lower — estimate refill cost
            # kWh needed from grid = (soc_gap% × capacity) / charge_efficiency
            refill_kwh = (soc_gap / 100) * BATTERY_CAPACITY_KWH / CHARGE_EFFICIENCY
            # Estimate refill price: use avg of cheapest hours seen today,
            # or fall back to 2¢ energy (typical overnight)
            cheap_prices = sorted([h["avg_price"] for h in hours if h["avg_price"] > 0])[:4]
            if cheap_prices:
                est_refill_energy = sum(cheap_prices) / len(cheap_prices)
            else:
                est_refill_energy = 2.0  # fallback: typical cheap overnight
            est_refill_total = est_refill_energy + arb_config.TD_RATE
            refill_cost = refill_kwh * est_refill_total
        elif soc_gap < 0:
            # Arbiter has MORE battery than actual — credit the saved energy
            # (it won't need to charge as much next cycle)
            saved_kwh = (abs(soc_gap) / 100) * BATTERY_CAPACITY_KWH / CHARGE_EFFICIENCY
            cheap_prices = sorted([h["avg_price"] for h in hours if h["avg_price"] > 0])[:4]
            if cheap_prices:
                est_price = sum(cheap_prices) / len(cheap_prices)
            else:
                est_price = 2.0
            refill_cost = -(saved_kwh * (est_price + arb_config.TD_RATE))
            refill_kwh = -saved_kwh

    # ── Totals ──────────────────────────────────────────────────────────
    arbiter_total_with_refill = total_arbiter_cost + refill_cost
    savings = total_manual_cost - arbiter_total_with_refill
    flat_rate_cost = total_manual_grid_kwh * (arb_config.TARGET_ENERGY_RATE + arb_config.TD_RATE)

    totals = {
        "manual_cost": round(total_manual_cost, 2),
        "manual_grid_kwh": round(total_manual_grid_kwh, 3),
        "manual_avg_rate": round(total_manual_cost / total_manual_grid_kwh, 2) if total_manual_grid_kwh > 0 else 0,
        "arbiter_cost": round(total_arbiter_cost, 2),
        "arbiter_grid_kwh": round(total_arbiter_grid_kwh, 3),
        "arbiter_avg_rate": round(total_arbiter_cost / total_arbiter_grid_kwh, 2) if total_arbiter_grid_kwh > 0 else 0,
        "refill_soc_gap": round(soc_gap, 1),
        "refill_kwh": round(refill_kwh, 2),
        "refill_cost": round(refill_cost, 2),
        "arbiter_total_with_refill": round(arbiter_total_with_refill, 2),
        "savings_cents": round(savings, 2),
        "flat_rate_cost": round(flat_rate_cost, 2),
        "starting_soc": round(starting_soc, 1),
        "ending_soc": round(sim_soc, 1),
        "actual_ending_soc": round(actual_ending_soc, 1) if actual_ending_soc is not None else None,
        "battery_cost_used": round(sim_battery_cost_pool, 2),
    }

    return {"date": date_str, "hours": hours, "totals": totals}


def _estimate_starting_soc(date_str: str) -> float | None:
    """Try to get SOC at midnight from the arbiter CSV."""
    log_file = os.path.join(_PROJECT_DIR, "logs", "arbiter.csv")
    if not os.path.exists(log_file):
        return None

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            # Find the earliest entry for this date
            for row in reader:
                ts = row.get("timestamp", "")
                if ts.startswith(date_str):
                    soc = row.get("soc_pct")
                    if soc and soc != "":
                        try:
                            return float(soc)
                        except ValueError:
                            pass
                    break
    except Exception:
        pass
    return None
