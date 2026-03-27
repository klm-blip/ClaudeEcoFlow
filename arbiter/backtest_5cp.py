#!/usr/bin/env python3
"""Run the 5-CP backtester and display results.

Usage:
    python -m arbiter.backtest_5cp              # All years 2019-2025
    python -m arbiter.backtest_5cp 2024         # Single year
    python -m arbiter.backtest_5cp 2023 2024    # Specific years
"""

import json
import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("arbiter.capacity")

from .capacity import backtest_year, backtest_all, KNOWN_5CP


def print_year_results(result: dict):
    """Pretty-print a single year's backtest results."""
    year = result["year"]
    if "error" in result:
        print(f"\n{'='*70}")
        print(f"  {year}: {result['error']}")
        return

    acc = result["accuracy"]
    tiers = result["tier_counts"]

    print(f"\n{'='*70}")
    print(f"  BACKTEST: Summer {year}")
    print(f"{'='*70}")

    print(f"\n  Days scored: {result['total_days_scored']}")
    print(f"  Tiers: HIGH={tiers.get('HIGH',0)}  MEDIUM={tiers.get('MEDIUM',0)}  "
          f"LOW={tiers.get('LOW',0)}  NEGLIGIBLE={tiers.get('NEGLIGIBLE',0)}")

    print(f"\n  Accuracy (catching {acc['total_known']} known 5-CP days):")
    print(f"    In top  5 scored days: {acc['caught_in_top_5']}/5")
    print(f"    In top 10 scored days: {acc['caught_in_top_10']}/5")
    print(f"    In top 15 scored days: {acc['caught_in_top_15']}/5")
    print(f"    In top 20 scored days: {acc['caught_in_top_20']}/5")

    print(f"\n  Known 5-CP days and their scores:")
    print(f"  {'Date':<12} {'Score':>6} {'Tier':<12} {'Rank':>5} {'Avg High':>9} {'Hot Streak':>11}")
    print(f"  {'-'*12} {'-'*6} {'-'*12} {'-'*5} {'-'*9} {'-'*11}")
    for cp in result["known_5cp_results"]:
        print(f"  {cp['date']:<12} {cp['score']:>6.1f} {cp['tier']:<12} "
              f"#{cp['rank']:>4} {cp['avg_high']:>8.1f}F {cp['consecutive_hot']:>7}d")

    print(f"\n  Top 20 scored days:")
    print(f"  {'#':>3} {'Date':<12} {'Day':<10} {'Score':>6} {'Tier':<8} "
          f"{'Avg Hi':>7} {'Max Hi':>7} {'Streak':>6} {'5-CP?':>6}")
    print(f"  {'-'*3} {'-'*12} {'-'*10} {'-'*6} {'-'*8} "
          f"{'-'*7} {'-'*7} {'-'*6} {'-'*6}")
    for i, d in enumerate(result["top_20_days"], 1):
        marker = " <<< " if d["is_5cp"] else ""
        print(f"  {i:>3} {d['date']:<12} {d['weekday']:<10} {d['score']:>6.1f} {d['tier']:<8} "
              f"{d['avg_high']:>6.1f}F {d['max_high']:>6.1f}F {d['hot_streak']:>5}d "
              f"{'YES' if d['is_5cp'] else '':>5}{marker}")


def print_aggregate(aggregate: dict):
    """Print aggregate results across all years."""
    print(f"\n{'='*70}")
    print(f"  AGGREGATE RESULTS ({aggregate['years_tested']} summers)")
    print(f"{'='*70}")
    print(f"\n  Total known 5-CP peaks: {aggregate['total_known_peaks']}")
    print(f"  Caught in top 10/year:  {aggregate['catch_rate_top_10']} "
          f"({aggregate['caught_in_top_10_per_year']}/{aggregate['total_known_peaks']})")
    print(f"  Caught in top 15/year:  {aggregate['catch_rate_top_15']} "
          f"({aggregate['caught_in_top_15_per_year']}/{aggregate['total_known_peaks']})")
    print(f"  Caught in top 20/year:  {aggregate['catch_rate_top_20']} "
          f"({aggregate['caught_in_top_20_per_year']}/{aggregate['total_known_peaks']})")
    print(f"  Lowest 5-CP score:      {aggregate['lowest_cp_score_seen']}")
    print(f"\n  Recommendation: {aggregate['recommendation']}")


def main():
    years = None
    if len(sys.argv) > 1:
        years = [int(y) for y in sys.argv[1:]]

    start = time.time()

    if years and len(years) == 1:
        result = backtest_year(years[0])
        print_year_results(result)
    else:
        results = backtest_all(years)
        for year, result in sorted(results["per_year"].items()):
            print_year_results(result)
        print_aggregate(results["aggregate"])

    elapsed = time.time() - start
    print(f"\n  Completed in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
