"""Live 5-CP capacity scoring for the running Arbiter.

Wraps capacity.score_day() with:
  - Daily-cached forecast fetch from Open-Meteo (no API key required)
  - Lookback for heat-wave detection (uses prior 4 days from same fetch)
  - Safe failure: returns NEGLIGIBLE tier on any error so Arbiter
    falls back to normal profitability logic.

Use:
    score = get_today_score()
    if score and score.tier in ("HIGH", "MEDIUM"):
        ...peak-defense logic...
"""

import datetime
import json
import logging
import urllib.request

from . import config
from .capacity import (
    PJM_CITIES,
    score_day,
    DayScore,
    _weighted_avg,
)

log = logging.getLogger("arbiter.capacity_live")

# Day-level cache: {date.isoformat(): DayScore}
_score_cache: dict[str, DayScore] = {}
# Per-day fetched temps cache (so we don't refetch on every poll):
# {date.isoformat(): {city: temp_F}}
_temps_cache: dict[str, dict[str, float]] = {}
_last_fetch_date: datetime.date | None = None


def _fetch_forecast_window(today: datetime.date) -> dict[str, dict[str, float]]:
    """Fetch daily max temps for all PJM cities from -4 days through +1 day.

    Open-Meteo's free forecast API returns past_days + forecast_days in F.
    Returns: {date_str: {city: temp_F}}
    """
    out: dict[str, dict[str, float]] = {}
    for city, (lat, lon) in PJM_CITIES.items():
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max"
            f"&past_days=4&forecast_days=2"
            f"&timezone=America/New_York"
            f"&temperature_unit=fahrenheit"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ecoflow-arbiter/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            dates = data["daily"]["time"]
            temps = data["daily"]["temperature_2m_max"]
            for d, t in zip(dates, temps):
                if t is None:
                    continue
                out.setdefault(d, {})[city] = float(t)
        except Exception as e:
            log.warning("capacity: failed to fetch %s: %s", city, e)
    return out


def _refresh_if_needed(today: datetime.date) -> None:
    """Refetch forecast at most once per day."""
    global _last_fetch_date, _temps_cache, _score_cache
    if _last_fetch_date == today and today.isoformat() in _temps_cache:
        return
    log.info("capacity: refreshing forecast for %s", today.isoformat())
    fetched = _fetch_forecast_window(today)
    if not fetched:
        log.warning("capacity: forecast fetch returned no data")
        return
    _temps_cache = fetched
    _last_fetch_date = today
    # Invalidate scores so they get recomputed against the fresh temps
    _score_cache.clear()


def get_today_score() -> DayScore | None:
    """Return the 5-CP score for today, fetching/scoring as needed.

    Returns None on total fetch failure (caller should treat as NEGLIGIBLE).
    """
    today = datetime.date.today()

    # Out-of-season — skip the network call entirely
    if today.month not in config.CP_ACTIVE_MONTHS:
        return DayScore(
            date=today.isoformat(), score=0, tier="NEGLIGIBLE",
            avg_high=0, max_city_high=0, consecutive_hot=0,
            month_bonus=0, weekday=today.strftime("%A"),
            is_actual_5cp=False, components={"reason": "out of season"},
        )

    try:
        _refresh_if_needed(today)
    except Exception as e:
        log.warning("capacity: refresh failed: %s", e)

    cached = _score_cache.get(today.isoformat())
    if cached is not None:
        return cached

    today_temps = _temps_cache.get(today.isoformat())
    if not today_temps:
        return None

    # Build prev-days list (most recent first) for heat-wave detection
    prev_days = []
    for offset in range(1, 5):
        d = (today - datetime.timedelta(days=offset)).isoformat()
        if d in _temps_cache:
            prev_days.append(_temps_cache[d])

    score = score_day(today, today_temps, prev_days)
    _score_cache[today.isoformat()] = score
    log.info(
        "capacity: %s score=%.0f tier=%s avg_high=%.0fF consecutive_hot=%d",
        score.date, score.score, score.tier, score.avg_high, score.consecutive_hot,
    )
    return score


def in_peak_window(now: datetime.datetime = None) -> bool:
    """True if current hour is within the PJM coincident peak window."""
    h = (now or datetime.datetime.now()).hour
    return config.CP_PEAK_HOUR_START <= h <= config.CP_PEAK_HOUR_END
