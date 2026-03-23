"""Layer 1: Profitability Gate with Discharge Willingness Model.

Charge decisions respect the dashboard's SOC-banded thresholds.
Discharge decisions use a willingness model that considers:
  - Spread (grid cost vs battery delivered cost)
  - SOC level (low SOC → more reluctant)
  - Time of day (peak hours → more willing, cheap hours → save for later)
  - Outage reserve floor (never discharge below this)
  - Spike override (always discharge above threshold)
"""

import datetime

from . import config


# ── Charge band logic (reads dashboard thresholds) ─────────────────────────

def _get_charge_band(soc: float, thresholds: dict) -> tuple[str, float, int]:
    """Determine which SOC charge band applies and return (name, price_cap, rate).

    Uses the dashboard's existing band thresholds:
      Emergency 0-20%:  charge below emergency_below at emergency charge rate
      Low 20-60%:       charge below low_below at low charge rate
      Mid 60-85%:       charge below mid_below at mid charge rate
      High 85-100%:     charge below high_below at high charge rate
    """
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


# ── Discharge willingness model ────────────────────────────────────────────

def _get_soc_penalty(soc: float, thresholds: dict) -> tuple[float, str]:
    """Return (penalty_cents, band_label) based on SOC level.

    Uses configurable bands from dashboard thresholds if available,
    falls back to config defaults.
    """
    # Read customizable SOC willingness bands from thresholds
    # Format in thresholds: arbiter_willingness_soc_bands = [[80,0],[60,1],[40,3],[0,8]]
    bands = thresholds.get("arbiter_willingness_soc_bands")
    if bands and isinstance(bands, list):
        soc_bands = [(b[0], b[1]) for b in bands]
    else:
        soc_bands = config.WILLINGNESS_SOC_BANDS

    for floor_pct, penalty in soc_bands:
        if soc >= floor_pct:
            return penalty, f">={floor_pct}%"

    # Below all floors — use last band's penalty
    last_penalty = soc_bands[-1][1] if soc_bands else 8.0
    return last_penalty, f"<{soc_bands[-1][0] if soc_bands else 40}%"


def _get_timing_adjustment(override_hour: int = None, override_weekday: int = None) -> tuple[float, str]:
    """Return (adjustment_cents, period_label) based on time of day.

    Negative = more willing to discharge (peak hours).
    Positive = less willing (save for later).

    Args:
        override_hour: If set, use this hour instead of current time (for simulation)
        override_weekday: If set, use this weekday (0=Mon, 6=Sun)
    """
    if override_hour is not None:
        hour = override_hour
        weekday = override_weekday if override_weekday is not None else datetime.datetime.now().weekday()
    else:
        now = datetime.datetime.now()
        hour = now.hour
        weekday = now.weekday()  # 0=Monday, 6=Sunday

    # Evening peak: 5 PM (17) through 11 PM (23)
    if 17 <= hour <= 23:
        return config.TIMING_EVENING_PEAK, "evening-peak"

    # Morning peak: 5-8 AM weekdays only
    if 5 <= hour < 8 and weekday < 5:
        return config.TIMING_MORNING_PEAK, "morning-peak"

    # Overnight cheap: midnight (0) through 5 AM
    if hour < 5:
        return config.TIMING_OVERNIGHT_CHEAP, "overnight-cheap"

    # Neutral hours (8 AM - 5 PM)
    return 0.0, "neutral"


def _discharge_willingness(soc: float, thresholds: dict,
                           override_hour: int = None, override_weekday: int = None) -> tuple[float, str]:
    """Calculate the spread (cents) required before we'll discharge.

    Returns (required_spread, explanation_string).

    Formula: spread_needed = base_margin + soc_penalty + timing_adjustment
    """
    base = config.SAFETY_MARGIN_CENTS
    soc_penalty, soc_label = _get_soc_penalty(soc, thresholds)
    timing_adj, timing_label = _get_timing_adjustment(override_hour, override_weekday)

    required = base + soc_penalty + timing_adj
    explanation = (
        f"base {base:.1f} + SOC {soc_penalty:+.1f} ({soc_label}) "
        f"+ time {timing_adj:+.1f} ({timing_label}) = {required:.1f}¢"
    )
    return required, explanation


# ── Main evaluation ────────────────────────────────────────────────────────

def evaluate(state: dict, override_hour: int = None, override_weekday: int = None) -> tuple[str, str]:
    """Evaluate the profitability gate with discharge willingness.

    Args:
        state: Full dashboard state from /api/state
        override_hour: If set, use this hour for timing (for simulation)
        override_weekday: If set, use this weekday (for simulation)

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

    # ── Outage reserve floor ──────────────────────────────────────────
    outage_reserve = thresholds.get("outage_reserve_pct", 20.0)
    min_discharge_soc = max(config.MIN_DISCHARGE_SOC, outage_reserve)

    # ── SOC charge band ───────────────────────────────────────────────
    band_name, band_price_cap, band_rate = _get_charge_band(soc, thresholds)
    max_soc = thresholds.get("max_soc", 95)

    # ── Decision logic ────────────────────────────────────────────────

    # Battery full — no charging possible
    if soc >= max_soc:
        pass  # fall through to discharge evaluation

    # Hard floor: never discharge below outage reserve
    if soc <= min_discharge_soc:
        # Can only charge or hold
        if soc < max_soc and energy_price <= band_price_cap:
            return "charge", (
                f"FLOOR SOC {soc:.0f}% <= {min_discharge_soc:.0f}% reserve "
                f"[{band_name}] energy {energy_price:.1f}¢ "
                f"<= {band_price_cap:.1f}¢ -> charge at {band_rate}W"
            )
        return "backup", (
            f"FLOOR SOC {soc:.0f}% <= {min_discharge_soc:.0f}% reserve "
            f"| energy {energy_price:.1f}¢ > {band_name} cap {band_price_cap:.1f}¢ -> hold"
        )

    # Spike override: always discharge above spike threshold (regardless of willingness)
    spike_total = config.SPIKE_ENERGY_PRICE + config.TD_RATE
    if energy_price >= config.SPIKE_ENERGY_PRICE:
        return "discharge", (
            f"SPIKE: energy {energy_price:.1f}¢ >= {config.SPIKE_ENERGY_PRICE:.1f}¢ "
            f"-> always discharge (SOC {soc:.0f}%)"
        )

    # No battery cost data yet: use conservative fallback
    if eff_battery_cost <= 0:
        discharge_above = thresholds.get("discharge_above", 7.0)
        if energy_price >= discharge_above:
            return "discharge", (
                f"NO COST DATA: energy {energy_price:.1f}¢ >= {discharge_above:.1f}¢ threshold "
                f"-> discharge (SOC {soc:.0f}%)"
            )
        if soc < max_soc and energy_price <= band_price_cap:
            return "charge", (
                f"NO COST DATA [{band_name}]: energy {energy_price:.1f}¢ "
                f"<= {band_price_cap:.1f}¢ -> charge at {band_rate}W (SOC {soc:.0f}%)"
            )
        return "hold", (
            f"NO COST DATA: energy {energy_price:.1f}¢ "
            f"| {band_name} cap {band_price_cap:.1f}¢ | SOC {soc:.0f}%"
        )

    # ── Discharge willingness gate ────────────────────────────────────
    spread = total_grid_cost - eff_battery_cost
    required_spread, willingness_detail = _discharge_willingness(soc, thresholds, override_hour, override_weekday)

    # Refill-aware check: don't discharge unless the current total grid price
    # is above what it would cost to refill the battery at cheap rates.
    # Expected refill cost = (cheap_energy + T&D) / roundtrip_eff
    # Using the charge band price cap as the "cheap energy" estimate:
    # if we'd only charge below band_price_cap, refill cost is at least that.
    expected_refill_energy = max(band_price_cap, 0)  # what we'd pay to recharge
    expected_refill_total = (expected_refill_energy + config.TD_RATE) / roundtrip_eff
    refill_spread = total_grid_cost - expected_refill_total

    # DISCHARGE: spread exceeds willingness threshold AND above energy floor
    # AND current price is above expected refill cost (don't discharge to refill at same price)
    if (spread > required_spread and energy_price >= config.MIN_DISCHARGE_ENERGY_PRICE
            and refill_spread > required_spread):
        return "discharge", (
            f"DISCHARGE: grid {total_grid_cost:.1f}¢ vs batt {eff_battery_cost:.1f}¢ "
            f"(spread {spread:.1f}¢ > needed {required_spread:.1f}¢) "
            f"refill {expected_refill_total:.1f}¢ "
            f"[{willingness_detail}] | SOC {soc:.0f}%"
        )

    # CHARGE: only if price is below the SOC band threshold AND battery not full
    if soc < max_soc and energy_price <= band_price_cap:
        charge_cost_per_kwh = total_grid_cost / roundtrip_eff
        return "charge", (
            f"CHARGE [{band_name}]: energy {energy_price:.1f}¢ "
            f"<= {band_price_cap:.1f}¢ band cap "
            f"(delivered cost: {charge_cost_per_kwh:.1f}¢) "
            f"| {band_rate}W | SOC {soc:.0f}%"
        )

    # HOLD: not worth discharging, too expensive to charge
    hold_reasons = []
    if spread <= required_spread:
        hold_reasons.append(f"spread {spread:.1f}¢ <= needed {required_spread:.1f}¢")
    if refill_spread <= required_spread:
        hold_reasons.append(f"refill spread {refill_spread:.1f}¢ <= needed {required_spread:.1f}¢ (refill cost {expected_refill_total:.1f}¢)")
    if energy_price < config.MIN_DISCHARGE_ENERGY_PRICE:
        hold_reasons.append(f"energy {energy_price:.1f}¢ < floor {config.MIN_DISCHARGE_ENERGY_PRICE:.1f}¢")
    return "hold", (
        f"HOLD: grid {total_grid_cost:.1f}¢ vs batt {eff_battery_cost:.1f}¢ | "
        + " | ".join(hold_reasons) +
        f" [{willingness_detail}] | {band_name} needs <={band_price_cap:.1f}¢ | SOC {soc:.0f}%"
    )
