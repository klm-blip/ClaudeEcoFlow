"""CSV logging for price and command history."""

import csv
import datetime
import logging
import os
import threading

log = logging.getLogger("ecoflow")

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG_DIR = os.path.join(_PROJECT_DIR, "logs")
_lock = threading.Lock()


def _ensure_dir():
    os.makedirs(_LOG_DIR, exist_ok=True)


def _csv_path(prefix: str) -> str:
    return os.path.join(_LOG_DIR, f"{prefix}_{datetime.date.today().isoformat()}.csv")


def _write_row(prefix: str, headers: list, row: list):
    """Append a row to the daily CSV file, writing headers if new file."""
    _ensure_dir()
    path = _csv_path(prefix)
    is_new = not os.path.exists(path)
    try:
        with _lock:
            with open(path, "a", newline="") as f:
                w = csv.writer(f)
                if is_new:
                    w.writerow(headers)
                w.writerow(row)
    except Exception as e:
        log.warning("CSV log write failed (%s): %s", prefix, e)


_PRICE_HEADERS = ["timestamp", "price_5min", "price_hour", "effective_price", "running_avg", "tier", "trend"]

def log_price(price_state):
    """Log current price data to daily CSV."""
    ps = price_state
    now = datetime.datetime.now().isoformat(timespec="seconds")
    row = [
        now,
        f"{ps.price_5min:.2f}" if ps.price_5min is not None else "",
        f"{ps.price_hour:.2f}" if ps.price_hour is not None else "",
        f"{ps.effective_price:.2f}" if ps.effective_price is not None else "",
        f"{ps.running_hour_avg:.2f}" if ps.running_hour_avg is not None else "",
        ps.tier or "",
        ps.trend or "",
    ]
    _write_row("prices", _PRICE_HEADERS, row)


_CMD_HEADERS = ["timestamp", "live_dry", "command_type", "details", "soc_pct", "effective_price", "battery_w"]

def log_command(text: str, commands_live: bool, power_state, price_state):
    """Log a command to daily CSV with context."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    # Parse command type from text
    cmd_type = "unknown"
    if "MODE" in text:
        cmd_type = "mode"
    elif "CHARGE START" in text or "CHARGE MID" in text or "CHARGE LOW" in text or "CHARGE HIGH" in text or "EMERGENCY" in text:
        cmd_type = "charge_start"
    elif "CHARGE STOP" in text:
        cmd_type = "charge_stop"
    elif "RATE" in text:
        cmd_type = "rate_change"
    elif "DISCHARGE" in text:
        cmd_type = "discharge"
    elif "HOLD" in text:
        cmd_type = "hold"
    elif "AUTO" in text:
        cmd_type = "auto_toggle"
    elif "THRESHOLD" in text:
        cmd_type = "threshold"
    elif "COMMANDS" in text:
        cmd_type = "live_toggle"

    ps = price_state
    pw = power_state
    row = [
        now,
        "LIVE" if commands_live else "DRY",
        cmd_type,
        text,
        f"{pw.soc_pct:.0f}" if pw.soc_pct is not None else "",
        f"{ps.effective_price:.2f}" if ps.effective_price is not None else "",
        f"{pw.battery_w:.0f}" if pw.battery_w is not None else "",
    ]
    _write_row("commands", _CMD_HEADERS, row)
