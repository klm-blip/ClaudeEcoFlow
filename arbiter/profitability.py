"""Layer 1: Profitability Gate — should we charge, discharge, or hold?

Uses battery cost pool data + efficiency measurements from the dashboard
to determine whether discharging is profitable vs buying from the grid.
"""

from . import config


def evaluate(state: dict) -> tuple[str, str]:
    """Evaluate the profitability gate.

    Args:
        state: Full dashboard state from /api/state

    Returns:
        (action, reason) where action is one of:
        - "discharge": self-powered mode, battery powers home
        - "charge":    backup mode + charge from grid
        - "backup":    backup mode, no charging (hold)
        - "hold":      no action needed (current state is fine)
    """
    price = state.get("price", {})
    power = state.get("power", {})
    battery = state.get("battery_cost", {})
    thresholds = state.get("thresholds", {})

    # ── Extract values ─────────────────────────────────────────────────
    energy_price = price.get("effective_price")  # ComEd energy-only (cents)
    if energy_price is None:
        return "hold", "No price data available"

    soc = power.get("soc_pct")
    if soc is None:
        return "hold", "No SOC data (waiting for telemetry)"

    total_grid_cost = energy_price + config.TD_RATE  # full cost to buy from grid

    # Battery cost data
    avg_cost = battery.get("avg_cost_cents_kwh", 0)  # includes T&D from charge tracking
    effective_cost = battery.get("effective_cost_per_kwh", 0)  # uses assumed 81% roundtrip

    # Use hardwired roundtrip efficiency (matches dashboard's assumption)
    roundtrip_eff = config.DEFAULT_CHARGE_EFFICIENCY * config.DEFAULT_DISCHARGE_EFFICIENCY

    # Effective battery cost per usable kWh delivered to home
    if effective_cost > 0:
        eff_battery_cost = effective_cost  # dashboard already computed this with 81% RT
    elif avg_cost > 0:
        eff_battery_cost = avg_cost / roundtrip_eff  # compute it ourselves
    else:
        eff_battery_cost = 0  # no data yet, can't evaluate

    # ── Decision logic ──────────────────────────────────────────────────

    # Safety: low SOC → don't discharge
    if soc <= config.MIN_DISCHARGE_SOC:
        if energy_price <= config.MAX_CHARGE_ENERGY_PRICE:
            return "charge", (
                f"LOW SOC {soc:.0f}% + cheap energy {energy_price:.1f}c "
                f"(total {total_grid_cost:.1f}c) -> charge"
            )
        return "backup", f"LOW SOC {soc:.0f}% -> backup (energy {energy_price:.1f}c too expensive to charge)"

    # Spike override: always discharge above spike threshold
    if total_grid_cost >= config.SPIKE_OVERRIDE_TOTAL_PRICE:
        return "discharge", (
            f"SPIKE: grid {total_grid_cost:.1f}c >= {config.SPIKE_OVERRIDE_TOTAL_PRICE:.1f}c "
            f"-> always discharge (SOC {soc:.0f}%)"
        )

    # No battery cost data yet: fall back to simple thresholds
    if eff_battery_cost <= 0:
        discharge_above = thresholds.get("discharge_above", 7.0)
        if energy_price >= discharge_above:
            return "discharge", (
                f"NO COST DATA: energy {energy_price:.1f}c >= {discharge_above:.1f}c threshold "
                f"-> discharge (SOC {soc:.0f}%)"
            )
        if energy_price <= config.MAX_CHARGE_ENERGY_PRICE:
            return "charge", (
                f"NO COST DATA: energy {energy_price:.1f}c <= {config.MAX_CHARGE_ENERGY_PRICE:.1f}c "
                f"-> charge (SOC {soc:.0f}%)"
            )
        return "hold", (
            f"NO COST DATA: energy {energy_price:.1f}c in dead zone -> hold (SOC {soc:.0f}%)"
        )

    # ── Profitability gate (the core logic) ────────────────────────────

    spread = total_grid_cost - eff_battery_cost
    breakeven_energy = eff_battery_cost - config.TD_RATE  # energy price at which grid = battery cost

    # DISCHARGE: grid cost exceeds battery cost + margin
    if spread > config.SAFETY_MARGIN_CENTS and energy_price >= config.MIN_DISCHARGE_ENERGY_PRICE:
        return "discharge", (
            f"PROFITABLE: grid {total_grid_cost:.1f}c vs battery {eff_battery_cost:.1f}c "
            f"(spread {spread:.1f}c > {config.SAFETY_MARGIN_CENTS:.1f}c margin) "
            f"| eff {roundtrip_eff*100:.0f}% | SOC {soc:.0f}%"
        )

    # Energy price below min discharge floor: definitely don't discharge
    if energy_price < config.MIN_DISCHARGE_ENERGY_PRICE:
        if energy_price <= config.MAX_CHARGE_ENERGY_PRICE:
            return "charge", (
                f"CHEAP: energy {energy_price:.1f}c <= {config.MAX_CHARGE_ENERGY_PRICE:.1f}c "
                f"(total {total_grid_cost:.1f}c) -> charge | SOC {soc:.0f}%"
            )
        return "backup", (
            f"CHEAP: energy {energy_price:.1f}c < discharge floor {config.MIN_DISCHARGE_ENERGY_PRICE:.1f}c "
            f"but > charge cap -> hold | SOC {soc:.0f}%"
        )

    # CHARGE: cheap energy
    if energy_price <= config.MAX_CHARGE_ENERGY_PRICE:
        charge_cost_per_kwh = total_grid_cost / roundtrip_eff
        return "charge", (
            f"CHARGE: energy {energy_price:.1f}c <= {config.MAX_CHARGE_ENERGY_PRICE:.1f}c "
            f"(cost/kWh stored: {charge_cost_per_kwh:.1f}c) | SOC {soc:.0f}%"
        )

    # HOLD: not profitable enough to discharge, too expensive to charge
    return "hold", (
        f"HOLD: grid {total_grid_cost:.1f}c vs battery {eff_battery_cost:.1f}c "
        f"(spread {spread:.1f}c <= {config.SAFETY_MARGIN_CENTS:.1f}c margin) "
        f"| breakeven energy {breakeven_energy:.1f}c | SOC {soc:.0f}%"
    )
