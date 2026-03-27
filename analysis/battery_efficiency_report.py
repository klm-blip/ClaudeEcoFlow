#!/usr/bin/env python3
"""
Battery efficiency analysis — reads monitor CSVs and produces aggregate report.

Run from project root:
  python analysis/battery_efficiency_report.py [--days 30] [--log-dir logs/]

Reads:
  logs/battery_monitor_daily.csv   — daily energy summaries
  logs/battery_monitor_sessions.csv — per-session charge/discharge details
"""

import argparse
import csv
import os
import sys
from datetime import datetime, timedelta


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def analyze_daily(rows, days=None):
    """Aggregate daily summaries."""
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = [r for r in rows if r["date"] >= cutoff]

    if not rows:
        print("No daily data available.")
        return

    total_charge = sum(float(r["charge_wh"]) for r in rows)
    total_discharge = sum(float(r["discharge_wh"]) for r in rows)
    total_vampire = sum(float(r["vampire_wh"]) for r in rows)
    total_loss = sum(float(r["implied_loss_wh"]) for r in rows)
    total_hours = sum(float(r["hours_monitored"]) for r in rows)

    rt_eff = total_discharge / total_charge * 100 if total_charge > 0 else 0
    total_energy_accounted = total_discharge + total_vampire + total_loss

    print("=" * 60)
    print(f"  BATTERY EFFICIENCY REPORT — {len(rows)} days")
    print(f"  Period: {rows[0]['date']} to {rows[-1]['date']}")
    print(f"  Monitoring: {total_hours:.0f} hours ({total_hours/24:.1f} days)")
    print("=" * 60)
    print()
    print(f"  Energy charged (AC in):     {total_charge/1000:8.1f} kWh")
    print(f"  Energy discharged (AC out):  {total_discharge/1000:8.1f} kWh")
    print(f"  Vampire drain (estimated):   {total_vampire/1000:8.1f} kWh")
    print(f"  Implied conversion loss:     {total_loss/1000:8.1f} kWh")
    print()
    print(f"  Roundtrip efficiency:        {rt_eff:8.1f}%")
    print(f"    (discharge out / charge in)")
    print()

    if total_charge > 0:
        loss_pct = total_loss / total_charge * 100
        vamp_pct = total_vampire / total_charge * 100
        print(f"  Loss breakdown as % of charge energy:")
        print(f"    Conversion losses:         {loss_pct:8.1f}%")
        print(f"    Vampire drain:             {vamp_pct:8.1f}%")
        print(f"    Delivered to home:         {rt_eff:8.1f}%")
        print(f"    Total:                     {loss_pct + vamp_pct + rt_eff:8.1f}%")
    print()

    # Daily breakdown
    print("  Daily breakdown:")
    print(f"  {'Date':<12} {'Charge':>8} {'Discharge':>10} {'Vampire':>8} {'Loss':>8} {'Eff%':>6} {'SOC':>10}")
    print(f"  {'-'*12} {'-'*8} {'-'*10} {'-'*8} {'-'*8} {'-'*6} {'-'*10}")
    for r in rows:
        ch = float(r["charge_wh"]) / 1000
        di = float(r["discharge_wh"]) / 1000
        va = float(r["vampire_wh"]) / 1000
        lo = float(r["implied_loss_wh"]) / 1000
        eff = float(r["day_rt_eff_pct"])
        s_start = float(r["soc_start"])
        s_end = float(r["soc_end"])
        print(f"  {r['date']:<12} {ch:7.1f}  {di:9.1f}  {va:7.1f}  {lo:7.1f}  {eff:5.1f}  {s_start:3.0f}%→{s_end:3.0f}%")
    print()


def analyze_sessions(rows, days=None):
    """Analyze charge/discharge sessions."""
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = [r for r in rows if r["timestamp"] >= cutoff]

    if not rows:
        print("No session data available.")
        return

    charge_sessions = [r for r in rows if r["type"] == "charge"]
    discharge_sessions = [r for r in rows if r["type"] == "discharge"]

    print("=" * 60)
    print(f"  SESSION ANALYSIS — {len(rows)} sessions")
    print("=" * 60)
    print()

    for label, sessions in [("CHARGE (AC→DC)", charge_sessions),
                             ("DISCHARGE (DC→AC)", discharge_sessions)]:
        if not sessions:
            print(f"  {label}: no sessions")
            continue

        effs = [float(s["efficiency_pct"]) for s in sessions if float(s["efficiency_pct"]) > 0]
        durations = [float(s["duration_min"]) for s in sessions]
        whs = [float(s["ac_wh"]) for s in sessions]
        peaks = [float(s["peak_w"]) for s in sessions]

        print(f"  {label}: {len(sessions)} sessions")
        if effs:
            print(f"    Efficiency:  avg {sum(effs)/len(effs):.1f}%  "
                  f"min {min(effs):.1f}%  max {max(effs):.1f}%")
        print(f"    Duration:    avg {sum(durations)/len(durations):.0f}min  "
              f"min {min(durations):.0f}min  max {max(durations):.0f}min")
        print(f"    Energy:      avg {sum(whs)/len(whs)/1000:.1f}kWh  "
              f"total {sum(whs)/1000:.1f}kWh")
        print(f"    Peak power:  avg {sum(peaks)/len(peaks):.0f}W  max {max(peaks):.0f}W")

        # Efficiency by charge rate bucket
        if label.startswith("CHARGE"):
            buckets = {"<1kW": [], "1-3kW": [], "3-6kW": [], ">6kW": []}
            for s in sessions:
                peak = float(s["peak_w"])
                eff = float(s["efficiency_pct"])
                if eff <= 0:
                    continue
                if peak < 1000:
                    buckets["<1kW"].append(eff)
                elif peak < 3000:
                    buckets["1-3kW"].append(eff)
                elif peak < 6000:
                    buckets["3-6kW"].append(eff)
                else:
                    buckets[">6kW"].append(eff)

            has_data = any(v for v in buckets.values())
            if has_data:
                print(f"    Efficiency by charge rate:")
                for bucket, vals in buckets.items():
                    if vals:
                        print(f"      {bucket:>5}: {sum(vals)/len(vals):.1f}% ({len(vals)} sessions)")
        print()


def main():
    parser = argparse.ArgumentParser(description="Battery efficiency analysis")
    parser.add_argument("--days", type=int, default=None, help="Limit to last N days")
    parser.add_argument("--log-dir", default="logs", help="Log directory (default: logs/)")
    args = parser.parse_args()

    daily_path = os.path.join(args.log_dir, "battery_monitor_daily.csv")
    session_path = os.path.join(args.log_dir, "battery_monitor_sessions.csv")

    daily_rows = read_csv(daily_path)
    session_rows = read_csv(session_path)

    if not daily_rows and not session_rows:
        print(f"No monitor data found in {args.log_dir}/")
        print("The battery monitor needs to run for at least one day to produce data.")
        sys.exit(0)

    analyze_daily(daily_rows, args.days)
    analyze_sessions(session_rows, args.days)


if __name__ == "__main__":
    main()
