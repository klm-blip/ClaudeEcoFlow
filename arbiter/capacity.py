"""PJM 5-CP Peak Day Prediction — scoring algorithm + backtester.

Scores each June-September weekday on likelihood of being a PJM 5 coincident
peak day, using multi-city temperature forecasts as the primary signal.

The 5-CP determines capacity charges (PLC) for ComEd hourly pricing customers.
Missing a peak costs ~$20-30/kW-year; false positive costs ~$2-5 in battery cycling.
Algorithm errs on the side of discharging on marginal days.
"""

import datetime
import json
import logging
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("arbiter.capacity")

# ── PJM key cities (by load contribution) ────────────────────────────────────

PJM_CITIES = {
    "Washington DC":  (38.90, -77.04),
    "Philadelphia":   (39.95, -75.17),
    "Baltimore":      (39.29, -76.61),
    "Chicago":        (41.88, -87.63),
    "Columbus OH":    (39.96, -82.99),
    "Pittsburgh":     (40.44, -79.99),
    "Richmond VA":    (37.54, -77.44),
}

# Eastern cities get heavier weight (larger PJM load share)
CITY_WEIGHTS = {
    "Washington DC":  0.20,
    "Philadelphia":   0.18,
    "Baltimore":      0.15,
    "Chicago":        0.15,
    "Columbus OH":    0.12,
    "Pittsburgh":     0.10,
    "Richmond VA":    0.10,
}

# ── Known 5-CP dates (ground truth for backtesting) ─────────────────────────

KNOWN_5CP = {
    2025: ["2025-06-23", "2025-06-24", "2025-06-25", "2025-07-28", "2025-07-29"],
    2024: ["2024-07-16", "2024-07-15", "2024-08-01", "2024-06-21", "2024-08-28"],
    2023: ["2023-07-27", "2023-09-05", "2023-07-28", "2023-09-06", "2023-07-05"],
    2022: ["2022-07-20", "2022-07-21", "2022-07-22", "2022-08-03", "2022-08-04"],
    2021: ["2021-06-28", "2021-06-29", "2021-06-30", "2021-08-12", "2021-08-26"],
    2020: ["2020-07-20", "2020-07-27", "2020-08-04", "2020-08-12", "2020-08-27"],
    2019: ["2019-07-19", "2019-07-17", "2019-07-10", "2019-08-19", "2019-07-29"],
    2018: ["2018-08-28", "2018-09-04", "2018-06-18", "2018-09-05", "2018-08-27"],
}


# ── Scoring algorithm ────────────────────────────────────────────────────────

@dataclass
class DayScore:
    date: str
    score: float           # 0-100
    tier: str              # "HIGH", "MEDIUM", "LOW", "NEGLIGIBLE"
    avg_high: float        # weighted avg high temp across cities
    max_city_high: float   # hottest city
    consecutive_hot: int   # consecutive hot days ending on this date
    month_bonus: float     # July bonus
    weekday: str
    is_actual_5cp: bool    # ground truth (for backtesting)
    components: dict       # breakdown of score


def score_day(date: datetime.date, city_temps: dict[str, float],
              prev_days_temps: list[dict[str, float]] = None) -> DayScore:
    """Score a single day for 5-CP likelihood.

    Args:
        date: The date to score
        city_temps: {city_name: daily_high_F} for this date
        prev_days_temps: List of prior days' city_temps (most recent first),
                         used for heat wave detection. Up to 4 prior days.

    Returns:
        DayScore with 0-100 score and tier classification.
    """
    # ── Weekend/holiday check ─────────────────────────────────────────
    if date.weekday() >= 5:  # Sat/Sun
        return DayScore(
            date=date.isoformat(), score=0, tier="NEGLIGIBLE",
            avg_high=0, max_city_high=0, consecutive_hot=0,
            month_bonus=0, weekday=date.strftime("%A"),
            is_actual_5cp=False, components={"reason": "weekend"}
        )

    # July 4 is a holiday — PJM excludes it
    if date.month == 7 and date.day == 4:
        return DayScore(
            date=date.isoformat(), score=0, tier="NEGLIGIBLE",
            avg_high=0, max_city_high=0, consecutive_hot=0,
            month_bonus=0, weekday=date.strftime("%A"),
            is_actual_5cp=False, components={"reason": "holiday (July 4)"}
        )

    # ── Temperature score (0-55 points) ───────────────────────────────
    # Weighted average high across PJM cities
    weighted_sum = 0
    weight_total = 0
    for city, temp in city_temps.items():
        w = CITY_WEIGHTS.get(city, 0.1)
        weighted_sum += temp * w
        weight_total += w

    avg_high = weighted_sum / weight_total if weight_total > 0 else 0
    max_city_high = max(city_temps.values()) if city_temps else 0

    # Temperature scoring curve — starts at 82F to catch mild-weather peaks
    # (e.g. COVID 2020 where depressed demand meant lower temps still peaked)
    # Below 82F: 0 points
    # 82-87F: 0-10 points (linear)
    # 87-92F: 10-25 points (linear)
    # 92-97F: 25-40 points (linear)
    # 97F+: 40-55 points (diminishing)
    if avg_high < 82:
        temp_score = 0
    elif avg_high < 87:
        temp_score = (avg_high - 82) / 5 * 10
    elif avg_high < 92:
        temp_score = 10 + (avg_high - 87) / 5 * 15
    elif avg_high < 97:
        temp_score = 25 + (avg_high - 92) / 5 * 15
    else:
        temp_score = 40 + min(15, (avg_high - 97) / 5 * 15)

    # ── Heat wave bonus (0-20 points) ─────────────────────────────────
    # Consecutive days with avg_high >= 85F (lowered from 88F to catch
    # first-day-of-heatwave peaks like 2021-06-28)
    consecutive_hot = 1  # today counts if avg >= 85
    if avg_high >= 85 and prev_days_temps:
        for prev_temps in prev_days_temps:
            prev_avg = _weighted_avg(prev_temps)
            if prev_avg >= 85:
                consecutive_hot += 1
            else:
                break

    if consecutive_hot >= 4:
        heatwave_score = 20
    elif consecutive_hot == 3:
        heatwave_score = 15
    elif consecutive_hot == 2:
        heatwave_score = 8
    else:
        heatwave_score = 0

    # ── Month bonus (0-10 points) ─────────────────────────────────────
    # July has 60% of historical peaks
    month_bonus = {6: 3, 7: 10, 8: 5, 9: 2}.get(date.month, 0)

    # ── Breadth bonus (0-10 points) ───────────────────────────────────
    # How many cities are above 90F? Wider heat = higher system load
    cities_above_90 = sum(1 for t in city_temps.values() if t >= 90)
    cities_above_95 = sum(1 for t in city_temps.values() if t >= 95)
    breadth_score = min(10, cities_above_90 * 1.5 + cities_above_95 * 1.5)

    # ── Hottest city bonus (0-5 points) ───────────────────────────────
    # If any single city is extremely hot, add points even if average is moderate
    # (localized extreme heat can still drive high regional load)
    if max_city_high >= 100:
        hot_city_bonus = 5
    elif max_city_high >= 97:
        hot_city_bonus = 3
    elif max_city_high >= 94:
        hot_city_bonus = 1
    else:
        hot_city_bonus = 0

    # ── Total score ───────────────────────────────────────────────────
    raw_score = temp_score + heatwave_score + month_bonus + breadth_score + hot_city_bonus
    score = min(100, max(0, raw_score))

    # ── Tier classification ───────────────────────────────────────────
    if score >= 70:
        tier = "HIGH"        # Almost certain 5-CP candidate
    elif score >= 50:
        tier = "MEDIUM"      # Likely candidate, discharge recommended
    elif score >= 30:
        tier = "LOW"         # Possible, reserve battery
    else:
        tier = "NEGLIGIBLE"  # Normal operations

    # Check ground truth
    year = date.year
    is_actual = date.isoformat() in KNOWN_5CP.get(year, [])

    return DayScore(
        date=date.isoformat(),
        score=round(score, 1),
        tier=tier,
        avg_high=round(avg_high, 1),
        max_city_high=round(max_city_high, 1),
        consecutive_hot=consecutive_hot,
        month_bonus=month_bonus,
        weekday=date.strftime("%A"),
        is_actual_5cp=is_actual,
        components={
            "temp_score": round(temp_score, 1),
            "heatwave_score": round(heatwave_score, 1),
            "month_bonus": month_bonus,
            "breadth_score": round(breadth_score, 1),
            "hot_city_bonus": hot_city_bonus,
            "cities_above_90": cities_above_90,
            "cities_above_95": cities_above_95,
        }
    )


def _weighted_avg(city_temps: dict[str, float]) -> float:
    """Compute weighted average temperature across cities."""
    weighted_sum = 0
    weight_total = 0
    for city, temp in city_temps.items():
        w = CITY_WEIGHTS.get(city, 0.1)
        weighted_sum += temp * w
        weight_total += w
    return weighted_sum / weight_total if weight_total > 0 else 0


# ── Weather data fetching (Open-Meteo historical API) ────────────────────────

def fetch_summer_temps(year: int) -> dict[str, dict[str, float]]:
    """Fetch daily max temps for all PJM cities for June-Sept of a given year.

    Returns:
        {date_str: {city_name: max_temp_F, ...}, ...}
    """
    start = f"{year}-06-01"
    end = f"{year}-09-30"
    all_temps = {}  # date -> {city: temp}

    for city, (lat, lon) in PJM_CITIES.items():
        url = (
            f"https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={lat}&longitude={lon}"
            f"&start_date={start}&end_date={end}"
            f"&daily=temperature_2m_max"
            f"&timezone=America/New_York"
            f"&temperature_unit=fahrenheit"
        )
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())

            dates = data["daily"]["time"]
            temps = data["daily"]["temperature_2m_max"]

            for d, t in zip(dates, temps):
                if d not in all_temps:
                    all_temps[d] = {}
                all_temps[d][city] = t

            log.info("Fetched %s temps for %d (%d days)", city, year, len(dates))
        except Exception as e:
            log.warning("Failed to fetch %s temps for %d: %s", city, year, e)

    return all_temps


# ── Backtester ───────────────────────────────────────────────────────────────

def backtest_year(year: int, temps: dict[str, dict[str, float]] = None) -> dict:
    """Score every June-Sept day of a given year and evaluate against known 5-CP.

    Args:
        year: Year to backtest
        temps: Pre-fetched temps (if None, fetches from Open-Meteo)

    Returns:
        Dict with scored days, metrics, and known 5-CP results.
    """
    if temps is None:
        temps = fetch_summer_temps(year)

    if not temps:
        return {"year": year, "error": "No temperature data available"}

    sorted_dates = sorted(temps.keys())
    scored_days = []

    for i, date_str in enumerate(sorted_dates):
        date = datetime.date.fromisoformat(date_str)
        if date.month < 6 or date.month > 9:
            continue

        city_temps = temps[date_str]

        # Gather previous days' temps for heat wave detection
        prev_days = []
        for j in range(1, 5):
            prev_date = (date - datetime.timedelta(days=j)).isoformat()
            if prev_date in temps:
                prev_days.append(temps[prev_date])

        day_score = score_day(date, city_temps, prev_days)
        scored_days.append(day_score)

    # ── Evaluate prediction accuracy ──────────────────────────────────
    known_dates = set(KNOWN_5CP.get(year, []))

    # Sort by score descending
    ranked = sorted(scored_days, key=lambda d: d.score, reverse=True)

    # How many of the top-N scored days are actual 5-CP?
    top_5 = {d.date for d in ranked[:5]}
    top_10 = {d.date for d in ranked[:10]}
    top_15 = {d.date for d in ranked[:15]}
    top_20 = {d.date for d in ranked[:20]}

    caught_in_5 = len(known_dates & top_5)
    caught_in_10 = len(known_dates & top_10)
    caught_in_15 = len(known_dates & top_15)
    caught_in_20 = len(known_dates & top_20)

    # What scores did the actual 5-CP days get?
    cp_scores = []
    for d in scored_days:
        if d.is_actual_5cp:
            cp_scores.append({
                "date": d.date,
                "score": d.score,
                "tier": d.tier,
                "avg_high": d.avg_high,
                "consecutive_hot": d.consecutive_hot,
                "rank": next(
                    (i + 1 for i, r in enumerate(ranked) if r.date == d.date),
                    None
                ),
            })

    # Count days by tier
    tier_counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "NEGLIGIBLE": 0}
    for d in scored_days:
        tier_counts[d.tier] = tier_counts.get(d.tier, 0) + 1

    # Lowest-scoring actual 5-CP day (the one we'd most likely miss)
    min_cp_score = min((s["score"] for s in cp_scores), default=0)

    return {
        "year": year,
        "total_days_scored": len(scored_days),
        "tier_counts": tier_counts,
        "accuracy": {
            "caught_in_top_5": caught_in_5,
            "caught_in_top_10": caught_in_10,
            "caught_in_top_15": caught_in_15,
            "caught_in_top_20": caught_in_20,
            "total_known": len(known_dates),
        },
        "known_5cp_results": sorted(cp_scores, key=lambda x: x["score"], reverse=True),
        "min_cp_score": min_cp_score,
        "top_20_days": [
            {
                "date": d.date,
                "score": d.score,
                "tier": d.tier,
                "avg_high": d.avg_high,
                "max_high": d.max_city_high,
                "hot_streak": d.consecutive_hot,
                "weekday": d.weekday,
                "is_5cp": d.is_actual_5cp,
                "components": d.components,
            }
            for d in ranked[:20]
        ],
    }


def backtest_all(years: list[int] = None) -> dict:
    """Run backtester across multiple years and aggregate results.

    Args:
        years: List of years to test. Defaults to 2019-2025.

    Returns:
        Dict with per-year results and aggregate metrics.
    """
    if years is None:
        years = [2019, 2020, 2021, 2022, 2023, 2024, 2025]

    results = {}
    total_known = 0
    total_caught_10 = 0
    total_caught_15 = 0
    total_caught_20 = 0
    all_min_scores = []

    for year in years:
        log.info("Backtesting %d...", year)
        result = backtest_year(year)
        results[year] = result

        if "error" not in result:
            acc = result["accuracy"]
            total_known += acc["total_known"]
            total_caught_10 += acc["caught_in_top_10"]
            total_caught_15 += acc["caught_in_top_15"]
            total_caught_20 += acc["caught_in_top_20"]
            all_min_scores.append(result["min_cp_score"])

    # Aggregate
    aggregate = {
        "years_tested": len(years),
        "total_known_peaks": total_known,
        "caught_in_top_10_per_year": total_caught_10,
        "caught_in_top_15_per_year": total_caught_15,
        "caught_in_top_20_per_year": total_caught_20,
        "catch_rate_top_10": f"{total_caught_10}/{total_known}" if total_known else "N/A",
        "catch_rate_top_15": f"{total_caught_15}/{total_known}" if total_known else "N/A",
        "catch_rate_top_20": f"{total_caught_20}/{total_known}" if total_known else "N/A",
        "lowest_cp_score_seen": min(all_min_scores) if all_min_scores else None,
        "recommendation": "",
    }

    # Set threshold recommendation based on results
    if all_min_scores:
        lowest = min(all_min_scores)
        if lowest >= 50:
            aggregate["recommendation"] = (
                f"Threshold of 50+ catches all known peaks. "
                f"Lowest 5-CP score was {lowest:.0f}."
            )
        elif lowest >= 30:
            aggregate["recommendation"] = (
                f"Threshold of {lowest - 5:.0f}+ needed to catch all known peaks. "
                f"Consider 30+ as discharge threshold."
            )
        else:
            aggregate["recommendation"] = (
                f"WARNING: Lowest 5-CP score was only {lowest:.0f}. "
                f"Temperature alone may not be sufficient — consider adding price/load signals."
            )

    return {"per_year": results, "aggregate": aggregate}
