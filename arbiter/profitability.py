"""Layer 1: Profitability Gate — should we charge, discharge, or hold?

Uses battery cost pool data + efficiency measurements from the dashboard
to determine whether discharging is profitable vs buying from the grid.

Charge decisions respect the dashboard's SOC-banded thresholds — the
battery only charges when the price is cheap enough for the current SOC
band, not just "below 4¢".
"""

from . import config


def _get_charge_band(soc: float, thresholds: dict) -> tuple[str, float, int]:
    """Determine which SOC charge band applies and return (name, price_cap, rate).

    Uses the dashboard's existing band thresholds:
      Emergency 0-20%:  charge below emergency_below at emergency charge rate
      Low 20-60%:       charge below low_below at low charge rate
      Mid 60-85%:       charge below mid_below at mid charge rate
      High 85-100%:     charge below high_below at high charge rate
    """
    # Band boundaries (from dashboard AutoThresholds)
    high_floor = thresholds.get("high_floor", 85)
    mid_floor = thresholds.get("mid_floor", 60)
    low_floor = thresholds.get("low_floor", 20)

    if soc >= high_floor:
        return "HIGH", thresholds.get("high_charge_below", -1.0), int(thresholds.get("high_rate", 1500))
    elif soc >= mid_floor:
        return "MID", thresholds.get("mid_charge_below", 1.5), int(thresholds.get("mid_rate", 3000))
    elif soc >= low_floor:
        return "LOW", thresholds.get("low_charge_below", 2.0), int(thresholds.get("low_rate", 6000))
    else:
        return "EMERGENCY", thresholds.get("emergency_charge_below", 6.0), int(thresholds.get("rate_emergency", 6000))


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
    avg_cost = battery.get("avg_cost_cents_kwh", 0)
    effective_cost = battery.get("effective_cost_per_kwh", 0)  # uses assumed 81% roundtrip

    # Use hardwired roundtrip efficiency (matches dashboard's assumption)
    roundtrip_eff = config.DEFAULT_CHARGE_EFFICIENCY * config.DEFAULT_DISCHARGE_EFFICIENCY

    # Effective battery cost per usable kWh delivered to home
    if effective_cost > 0:
        eff_battery_cost = effective_cost
    elif avg_cost > 0:
        eff_battery_cost = avg_cost / roundtrip_eff
    else:
        eff_battery_cost = 0

    # ── SOC charge band ────────────────────────────────────────────────
    band_name, band_price_cap, band_rate = _get_charge_band(soc, thresholds)
    max_soc = thresholds.get("max_soc", 95)

    # ── Decision logic ──────────────────────────────────────────────────

    # Battery full — no charging possible
    if soc >= max_soc:
        # Can still evaluate discharge
        pass

    # Spike override: always discharge above spike threshold
    if total_grid_cost >= config.SPIKE_OVERRIDE_TOTAL_PRICE:
        return "discharge", (
            f"SPIKE: grid {total_grid_cost:.1f}c >= {config.SPIKE_OVERRIDE_TOTAL_PRICE:.1f}c "
            f"-> always discharge (SOC {soc:.0f}%)"
        )

    # Safety: very low SOC → charge aggressively if price is in band
    if soc <= config.MIN_DISCHARGE_SOC:
        if energy_price <= band_price_cap:
            return "charge", (
                f"LOW SOC {soc:.0f}% [{band_name}] energy {energy_price:.1f}c "
                f"<= {band_price_cap:.1f}c -> charge at {band_rate}W"
            )
        return "backup", (
            f"LOW SOC {soc:.0f}% but energy {energy_price:.1f}c "
            f"> {band_name} cap {band_price_cap:.1f}c -> wait for cheaper"
        )

    # No battery cost data yet: fall back to band thresholds
    if eff_battery_cost <= 0:
        discharge_above = thresholds.get("discharge_above", 7.0)
        if energy_price >= discharge_above:
            return "discharge", (
                f"NO COST DATA: energy {energy_price:.1f}c >= {discharge_above:.1f}c threshold "
                f"-> discharge (SOC {soc:.0f}%)"
            )
        if soc < max_soc and energy_price <= band_price_cap:
            return "charge", (
                f"NO COST DATA [{band_name}]: energy {energy_price:.1f}c "
                f"<= {band_price_cap:.1f}c -> charge at {band_rate}W (SOC {soc:.0f}%)"
            )
        return "hold", (
            f"NO COST DATA: energy {energy_price:.1f}c "
            f"| {band_name} cap {band_price_cap:.1f}c | SOC {soc:.0f}%"
        )

    # ── Profitability gate (the core logic) ────────────────────────────

    spread = total_grid_cost - eff_battery_cost
    breakeven_energy = eff_battery_cost - config.TD_RATE

    # DISCHARGE: grid cost exceeds battery cost + margin
    if spread > config.SAFETY_MARGIN_CENTS and energy_price >= config.MIN_DISCHARGE_ENERGY_PRICE:
        return "discharge", (
            f"PROFITABLE: grid {total_grid_cost:.1f}c vs battery {eff_battery_cost:.1f}c "
            f"(spread {spread:.1f}c > {config.SAFETY_MARGIN_CENTS:.1f}c margin) "
            f"| RT {roundtrip_eff*100:.0f}% | SOC {soc:.0f}%"
        )

    # CHARGE: only if price is below the SOC band threshold AND battery not full
    if soc < max_soc and energy_price <= band_price_cap:
        charge_cost_per_kwh = total_grid_cost / roundtrip_eff
        return "charge", (
            f"CHARGE [{band_name}]: energy {energy_price:.1f}c "
            f"<= {band_price_cap:.1f}c band cap "
            f"(delivered cost: {charge_cost_per_kwh:.1f}c) "
            f"| {band_rate}W | SOC {soc:.0f}%"
        )

    # HOLD: not profitable enough to discharge, too expensive to charge for this band
    return "hold", (
        f"HOLD: grid {total_grid_cost:.1f}c vs battery {eff_battery_cost:.1f}c "
        f"(spread {spread:.1f}c) | {band_name} band needs <={band_price_cap:.1f}c "
        f"| breakeven {breakeven_energy:.1f}c | SOC {soc:.0f}%"
    )
