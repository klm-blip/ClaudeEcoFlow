"""
ComEd 5-Minute Price Trend Analysis

Goal: Determine if clusters of mid-to-high 5-minute prices within an hour
are predictive of the full hour ending up expensive.

Specifically testing the hypothesis:
"When you see 3+ consecutive 5-minute readings above X cents mid-hour,
the remainder of the hour tends to stay high."

This matters because the dashboard acts on hourly averages (BESH billing),
but by the time the hourly average crosses the discharge threshold,
you've already consumed most of the hour on grid power.
"""

import json
import urllib.request
import datetime
import statistics
import time
from collections import defaultdict


def fetch_5min_prices(start_date: str, end_date: str) -> list:
    """Fetch 5-minute ComEd prices for a date range.

    Args:
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
    Returns:
        List of (datetime, price_cents) tuples, chronological order
    """
    # Convert dates to ComEd API format: YYYYMMDDhhmm
    start = start_date.replace("-", "") + "0000"
    end = end_date.replace("-", "") + "2355"

    url = f"https://hourlypricing.comed.com/api?type=5minutefeed&datestart={start}&dateend={end}"
    print(f"Fetching {start_date} to {end_date}...")

    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    # Convert to (datetime, price) tuples, chronological order
    records = []
    for entry in data:
        ts = datetime.datetime.fromtimestamp(int(entry["millisUTC"]) / 1000)
        price = float(entry["price"])
        records.append((ts, price))

    records.sort(key=lambda x: x[0])
    print(f"  Got {len(records)} records")
    return records


def group_by_hour(records: list) -> dict:
    """Group 5-minute records into hours.

    Returns:
        Dict of (date, hour) -> list of (minute_offset, price)
    """
    hours = defaultdict(list)
    for ts, price in records:
        key = (ts.date(), ts.hour)
        minute = ts.minute
        hours[key].append((minute, price))

    # Sort each hour's readings by minute
    for key in hours:
        hours[key].sort()

    return hours


def analyze_early_warning_signals(hours: dict,
                                   signal_threshold: float = 8.0,
                                   consecutive_count: int = 3,
                                   expensive_hour_threshold: float = 7.0):
    """
    For each hour, check if seeing N consecutive 5-min prices above a threshold
    early/mid-hour predicts the full hour being expensive.

    Args:
        signal_threshold: 5-min price above which counts as "elevated" (cents)
        consecutive_count: how many consecutive elevated readings = signal
        expensive_hour_threshold: hourly average above this = "expensive hour"
    """
    signal_fired = 0      # hours where the early signal fired
    signal_correct = 0    # signal fired AND hour was expensive
    signal_wrong = 0      # signal fired AND hour was NOT expensive

    no_signal = 0         # hours with no signal
    missed = 0            # no signal BUT hour was expensive
    true_negative = 0     # no signal AND hour was cheap (correct)

    total_hours = 0
    expensive_hours = 0

    # Track timing of signals
    signal_minutes = []   # what minute the signal first fires

    # Track how much money we'd save with early detection
    savings_analysis = []

    for (date, hour), readings in sorted(hours.items()):
        if len(readings) < 6:  # need at least half an hour of data
            continue

        total_hours += 1
        prices = [p for _, p in readings]
        hourly_avg = statistics.mean(prices)
        is_expensive = hourly_avg >= expensive_hour_threshold
        if is_expensive:
            expensive_hours += 1

        # Check for consecutive elevated readings
        signal_minute = None
        consecutive = 0
        for minute, price in readings:
            if price >= signal_threshold:
                consecutive += 1
                if consecutive >= consecutive_count and signal_minute is None:
                    signal_minute = minute
            else:
                consecutive = 0

        if signal_minute is not None:
            signal_fired += 1
            signal_minutes.append(signal_minute)
            if is_expensive:
                signal_correct += 1

                # Calculate: if we switched to battery at signal_minute,
                # how much grid consumption would we have avoided?
                pre_signal = [p for m, p in readings if m < signal_minute]
                post_signal = [p for m, p in readings if m >= signal_minute]
                if post_signal:
                    avg_post = statistics.mean(post_signal)
                    pct_hour_saved = len(post_signal) / len(readings)
                    savings_analysis.append({
                        "date": str(date),
                        "hour": hour,
                        "hourly_avg": hourly_avg,
                        "signal_at": signal_minute,
                        "avg_after_signal": avg_post,
                        "pct_hour_remaining": pct_hour_saved,
                    })
            else:
                signal_wrong += 1
        else:
            no_signal += 1
            if is_expensive:
                missed += 1
            else:
                true_negative += 1

    return {
        "params": {
            "signal_threshold": signal_threshold,
            "consecutive_count": consecutive_count,
            "expensive_hour_threshold": expensive_hour_threshold,
        },
        "total_hours": total_hours,
        "expensive_hours": expensive_hours,
        "signal_fired": signal_fired,
        "signal_correct": signal_correct,  # true positive
        "signal_wrong": signal_wrong,       # false positive
        "missed": missed,                   # false negative (expensive but no signal)
        "true_negative": true_negative,
        "precision": signal_correct / signal_fired if signal_fired else 0,
        "recall": signal_correct / expensive_hours if expensive_hours else 0,
        "avg_signal_minute": statistics.mean(signal_minutes) if signal_minutes else None,
        "median_signal_minute": statistics.median(signal_minutes) if signal_minutes else None,
        "savings_analysis": savings_analysis,
    }


def analyze_momentum(hours: dict, window: int = 3, momentum_threshold: float = 5.0,
                     expensive_hour_threshold: float = 7.0):
    """
    Alternative approach: look at the rate of change (momentum) of prices.
    If the rolling average over N readings rises above a threshold, fire signal.

    This catches the "ramp up" pattern even if individual readings haven't
    reached a high absolute level yet.
    """
    signal_fired = 0
    signal_correct = 0
    signal_wrong = 0
    missed = 0
    true_negative = 0
    total_hours = 0
    expensive_hours = 0
    signal_minutes = []

    for (date, hour), readings in sorted(hours.items()):
        if len(readings) < 6:
            continue

        total_hours += 1
        prices = [p for _, p in readings]
        hourly_avg = statistics.mean(prices)
        is_expensive = hourly_avg >= expensive_hour_threshold
        if is_expensive:
            expensive_hours += 1

        # Rolling window average
        signal_minute = None
        for i in range(window, len(readings)):
            window_prices = [readings[j][1] for j in range(i - window, i)]
            window_avg = statistics.mean(window_prices)
            if window_avg >= momentum_threshold and signal_minute is None:
                signal_minute = readings[i - 1][1]  # minute when signal fires
                signal_minute = readings[i - window][0]  # first minute of the window
                break

        if signal_minute is not None:
            signal_fired += 1
            signal_minutes.append(signal_minute)
            if is_expensive:
                signal_correct += 1
            else:
                signal_wrong += 1
        else:
            if is_expensive:
                missed += 1
            else:
                true_negative += 1

    return {
        "method": "momentum",
        "params": {
            "window": window,
            "momentum_threshold": momentum_threshold,
            "expensive_hour_threshold": expensive_hour_threshold,
        },
        "total_hours": total_hours,
        "expensive_hours": expensive_hours,
        "signal_fired": signal_fired,
        "signal_correct": signal_correct,
        "signal_wrong": signal_wrong,
        "missed": missed,
        "true_negative": true_negative,
        "precision": signal_correct / signal_fired if signal_fired else 0,
        "recall": signal_correct / expensive_hours if expensive_hours else 0,
        "avg_signal_minute": statistics.mean(signal_minutes) if signal_minutes else None,
    }


def analyze_spike_isolation(hours: dict, expensive_hour_threshold: float = 7.0):
    """
    Classify expensive hours into two categories:
    1. "Spike" hours: 1-2 extreme readings surrounded by low readings
       (hourly avg ends up high but it's just a blip)
    2. "Sustained" hours: multiple elevated readings that form a trend

    This tells us how much of the problem is spikes (can't predict)
    vs sustained highs (potentially predictable).
    """
    spike_hours = []
    sustained_hours = []

    for (date, hour), readings in sorted(hours.items()):
        if len(readings) < 6:
            continue

        prices = [p for _, p in readings]
        hourly_avg = statistics.mean(prices)

        if hourly_avg < expensive_hour_threshold:
            continue

        # Count how many readings are above various thresholds
        above_5 = sum(1 for p in prices if p >= 5.0)
        above_10 = sum(1 for p in prices if p >= 10.0)
        above_20 = sum(1 for p in prices if p >= 20.0)
        max_price = max(prices)

        # If removing the top 2 readings makes it cheap, it's a spike
        sorted_prices = sorted(prices)
        avg_without_top2 = statistics.mean(sorted_prices[:-2]) if len(sorted_prices) > 2 else hourly_avg

        entry = {
            "date": str(date),
            "hour": hour,
            "hourly_avg": round(hourly_avg, 1),
            "max_5min": round(max_price, 1),
            "readings_above_5": above_5,
            "readings_above_10": above_10,
            "readings_above_20": above_20,
            "avg_without_top2": round(avg_without_top2, 1),
            "total_readings": len(readings),
        }

        if avg_without_top2 < expensive_hour_threshold:
            entry["type"] = "spike"
            spike_hours.append(entry)
        else:
            entry["type"] = "sustained"
            sustained_hours.append(entry)

    return {
        "spike_hours": len(spike_hours),
        "sustained_hours": len(sustained_hours),
        "spike_pct": len(spike_hours) / (len(spike_hours) + len(sustained_hours)) * 100
            if (spike_hours or sustained_hours) else 0,
        "spike_examples": spike_hours[:10],
        "sustained_examples": sustained_hours[:10],
    }


def print_results(results: dict, label: str = ""):
    """Pretty-print analysis results."""
    p = results["params"]
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  Signal: {p.get('consecutive_count', p.get('window', '?'))} consecutive "
          f">= {p.get('signal_threshold', p.get('momentum_threshold', '?'))}c")
    print(f"  Expensive hour: avg >= {p['expensive_hour_threshold']}c")
    print(f"{'='*60}")
    print(f"  Total hours analyzed:     {results['total_hours']}")
    print(f"  Expensive hours:          {results['expensive_hours']} "
          f"({results['expensive_hours']/results['total_hours']*100:.1f}%)")
    print(f"  Signal fired:             {results['signal_fired']} times")
    print(f"  True positives:           {results['signal_correct']} "
          f"(signal fired, hour was expensive)")
    print(f"  False positives:          {results['signal_wrong']} "
          f"(signal fired, hour was cheap)")
    print(f"  Missed:                   {results['missed']} "
          f"(expensive hour, no signal)")
    print(f"  Precision:                {results['precision']:.1%} "
          f"(when signal fires, is it right?)")
    print(f"  Recall:                   {results['recall']:.1%} "
          f"(of expensive hours, how many caught?)")
    if results.get("avg_signal_minute") is not None:
        print(f"  Avg signal fires at:      minute {results['avg_signal_minute']:.0f}")
        print(f"  Median signal fires at:   minute {results.get('median_signal_minute', 'N/A')}")

    if results.get("savings_analysis"):
        avg_remaining = statistics.mean(s["pct_hour_remaining"] for s in results["savings_analysis"])
        avg_post_price = statistics.mean(s["avg_after_signal"] for s in results["savings_analysis"])
        print(f"\n  When signal is correct:")
        print(f"    Avg % of hour remaining:  {avg_remaining:.0%}")
        print(f"    Avg price after signal:   {avg_post_price:.1f}c")
        print(f"    (vs waiting for hourly avg to cross threshold)")


def main():
    print("ComEd 5-Minute Price Trend Analysis")
    print("=" * 60)

    # Fetch 2 weeks of data in chunks (API might limit range)
    all_records = []

    # Fetch week by week going back ~30 days
    end = datetime.date.today()
    start = end - datetime.timedelta(days=6)

    for week in range(4):  # 4 weeks
        week_end = end - datetime.timedelta(days=7 * week)
        week_start = week_end - datetime.timedelta(days=6)

        try:
            records = fetch_5min_prices(week_start.isoformat(), week_end.isoformat())
            all_records.extend(records)
            time.sleep(1)  # be polite to the API
        except Exception as e:
            print(f"  Error fetching {week_start} to {week_end}: {e}")

    if not all_records:
        print("No data fetched!")
        return

    # Deduplicate (overlapping ranges)
    seen = set()
    unique = []
    for ts, price in all_records:
        if ts not in seen:
            seen.add(ts)
            unique.append((ts, price))
    all_records = sorted(unique)

    print(f"\nTotal unique records: {len(all_records)}")
    print(f"Date range: {all_records[0][0]} to {all_records[-1][0]}")

    hours = group_by_hour(all_records)
    print(f"Total hours: {len(hours)}")

    # ── Analysis 1: Spike vs Sustained classification ──
    print("\n" + "=" * 60)
    print("  SPIKE vs SUSTAINED ANALYSIS")
    print("  (Are expensive hours driven by 1-2 extreme readings,")
    print("   or by sustained elevated prices?)")
    print("=" * 60)

    spike_analysis = analyze_spike_isolation(hours, expensive_hour_threshold=7.0)
    print(f"  Spike hours (1-2 readings cause it):    {spike_analysis['spike_hours']}")
    print(f"  Sustained hours (broadly elevated):     {spike_analysis['sustained_hours']}")
    print(f"  Spike %:                                {spike_analysis['spike_pct']:.0f}%")

    if spike_analysis['spike_examples']:
        print(f"\n  Example SPIKE hours (high avg but driven by 1-2 outliers):")
        for s in spike_analysis['spike_examples'][:5]:
            print(f"    {s['date']} {s['hour']:02d}:00 — avg {s['hourly_avg']}c, "
                  f"max {s['max_5min']}c, {s['readings_above_10']} readings >10c, "
                  f"avg-without-top2: {s['avg_without_top2']}c")

    if spike_analysis['sustained_examples']:
        print(f"\n  Example SUSTAINED hours (broadly elevated):")
        for s in spike_analysis['sustained_examples'][:5]:
            print(f"    {s['date']} {s['hour']:02d}:00 — avg {s['hourly_avg']}c, "
                  f"max {s['max_5min']}c, {s['readings_above_10']} readings >10c, "
                  f"avg-without-top2: {s['avg_without_top2']}c")

    # ── Analysis 2: Consecutive elevated readings ──
    # Test multiple parameter combinations
    test_configs = [
        # (signal_threshold, consecutive_count, label)
        (8.0, 2, "2 consecutive >= 8c"),
        (8.0, 3, "3 consecutive >= 8c"),
        (10.0, 2, "2 consecutive >= 10c"),
        (10.0, 3, "3 consecutive >= 10c"),
        (6.0, 3, "3 consecutive >= 6c"),
        (6.0, 4, "4 consecutive >= 6c"),
        (5.0, 4, "4 consecutive >= 5c"),
        (5.0, 5, "5 consecutive >= 5c"),
    ]

    print("\n" + "=" * 60)
    print("  EARLY WARNING SIGNAL ANALYSIS")
    print("  Testing: do consecutive elevated 5-min readings")
    print("  predict the hour will be expensive (avg >= 7c)?")
    print("=" * 60)

    for thresh, count, label in test_configs:
        results = analyze_early_warning_signals(
            hours,
            signal_threshold=thresh,
            consecutive_count=count,
            expensive_hour_threshold=7.0,
        )
        print_results(results, label)

    # ── Analysis 3: Momentum / rolling average ──
    print("\n" + "=" * 60)
    print("  MOMENTUM ANALYSIS")
    print("  Testing: does a rolling average of recent readings")
    print("  crossing a threshold predict expensive hours?")
    print("=" * 60)

    momentum_configs = [
        (3, 5.0, "3-reading avg >= 5c"),
        (3, 8.0, "3-reading avg >= 8c"),
        (4, 5.0, "4-reading avg >= 5c"),
        (4, 8.0, "4-reading avg >= 8c"),
    ]

    for window, thresh, label in momentum_configs:
        results = analyze_momentum(
            hours,
            window=window,
            momentum_threshold=thresh,
            expensive_hour_threshold=7.0,
        )
        p = results["params"]
        print(f"\n  {label}")
        print(f"    Precision: {results['precision']:.1%}  |  "
              f"Recall: {results['recall']:.1%}  |  "
              f"Fired: {results['signal_fired']}  |  "
              f"Missed: {results['missed']}")

    # ── Today's data detail view ──
    print("\n" + "=" * 60)
    print("  TODAY'S HOUR-BY-HOUR DETAIL")
    print("=" * 60)

    today = datetime.date.today()
    for (date, hour), readings in sorted(hours.items()):
        if date != today:
            continue
        prices = [p for _, p in readings]
        if not prices:
            continue
        avg = statistics.mean(prices)
        max_p = max(prices)

        # Build a mini sparkline
        bar = ""
        for _, p in readings:
            if p >= 20: bar += "\u2588"      # full block
            elif p >= 10: bar += "\u2593"     # dark shade
            elif p >= 5: bar += "\u2592"      # medium shade
            elif p >= 2: bar += "\u2591"      # light shade
            else: bar += "\u00b7"             # dot

        flag = " <<<" if avg >= 7.0 else ""
        print(f"  {hour:02d}:00  avg={avg:5.1f}c  max={max_p:5.1f}c  [{bar}]{flag}")


if __name__ == "__main__":
    main()
