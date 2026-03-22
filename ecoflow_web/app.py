"""
EcoFlow Web Dashboard — Flask + WebSocket server.

Run:
    cd "C:\\Users\\kmars\\OneDrive\\Desktop\\Claude EcoFlow Project"
    python -m ecoflow_web.app

Open http://localhost:5000 in your browser.
"""

import datetime
import json
import logging
import os
import threading
import time

from flask import Flask, send_from_directory, request
from flask_sock import Sock

from .config import COLORS, STATE_FILE, KIA_CREDENTIALS_FILE, ENPHASE_CREDENTIALS_FILE
from .state import PowerState, PriceState, KiaState, EnphaseState
from .history import HistoryBuffer
from .comed import ComedPoller
from .automation import AutoThresholds, AutoController
from .kia import KiaPoller
from .kia_automation import KiaAutoController
from .enphase import EnphasePoller
from .mqtt_handler import MQTTHandler
from .proto_codec import (
    build_mode_command, build_charge_command,
    build_charge_power_command, build_eps_command, build_and_wrap,
)
from . import logger
from .notify import TelegramNotifier
from .battery_cost import BatteryCostPool
from .energy_tracker import EnergyTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ecoflow")

# ─── Application state ─────────────────────────────────────────────────────
power_state  = PowerState()
price_state  = PriceState()
history      = HistoryBuffer()
thresholds   = AutoThresholds.load()
auto         = AutoController()
auto.enabled = True     # automation ON by default (production)

kia_state       = KiaState()
kia_auto        = KiaAutoController()
kia_auto.enabled = True   # EV automation ON by default (production)
kia_poller      = None    # set in main() if credentials exist

enphase_state   = EnphaseState()
enphase_poller  = None    # set in main() if credentials exist

commands_live   = True    # LIVE commands by default (production)
command_log     = []      # [{ts, live, text}, ...] last 30 entries
mqtt_handler    = None    # set in main()
notifier        = TelegramNotifier()
battery_pool    = BatteryCostPool()
energy_tracker  = EnergyTracker()
_state_lock     = threading.Lock()
_pool_initialized = False  # set True after first SOC-based init

# Load Telegram config from thresholds file if present
try:
    import os as _os
    from .config import THRESHOLDS_FILE as _TF
    if _os.path.exists(_TF):
        with open(_TF) as _f:
            notifier.load_from_thresholds(json.load(_f))
except Exception:
    pass

# WebSocket clients
_ws_clients = set()
_ws_lock    = threading.Lock()

# ─── Flask app ──────────────────────────────────────────────────────────────
_static_dir = os.path.join(os.path.dirname(__file__), "static")
app = Flask(__name__, static_folder=_static_dir)
sock = Sock(app)


@app.after_request
def add_no_cache_headers(response):
    """Prevent browser caching so refreshes always get the latest code."""
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/")
def index():
    return send_from_directory(_static_dir, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(_static_dir, filename)


# ─── Energy API ────────────────────────────────────────────────────────

@app.route("/api/energy")
def api_energy():
    """Return hourly energy data for a given date."""
    date_str = request.args.get("date", datetime.date.today().isoformat())
    rows = EnergyTracker.read_day(date_str)
    return json.dumps({"date": date_str, "hours": rows})


@app.route("/api/energy/summary")
def api_energy_summary():
    """Return aggregated energy data for a period (day/week/month)."""
    period = request.args.get("period", "day")
    today = datetime.date.today()
    if period == "week":
        start = today - datetime.timedelta(days=today.weekday())
    elif period == "month":
        start = today.replace(day=1)
    else:
        start = today
    data = EnergyTracker.summarize_period(start.isoformat(), today.isoformat())
    data["period"] = period
    data["start"] = start.isoformat()
    data["end"] = today.isoformat()
    return json.dumps(data)


@app.route("/api/energy/dates")
def api_energy_dates():
    """Return list of dates with energy data."""
    return json.dumps({"dates": EnergyTracker.available_dates()})


# ─── Arbiter API ──────────────────────────────────────────────────────────

@app.route("/api/state")
def api_state():
    """Full dashboard state for the Arbiter to read."""
    return _build_state_msg()


@app.route("/api/arbiter/action", methods=["POST"])
def api_arbiter_action():
    """Accept a command from the Arbiter.

    Body JSON:
        {"action": "discharge"}              → self-powered mode
        {"action": "charge", "rate": 3000}   → backup + charge at rate
        {"action": "backup"}                 → backup mode, stop charging
        {"action": "hold"}                   → no-op, just log the reason
        {"dry_run": true, ...}               → log only, don't execute

    Optional: "reason": "..." for logging.
    """
    data = request.get_json(silent=True) or {}
    action = data.get("action", "hold")
    reason = data.get("reason", f"Arbiter: {action}")
    dry_run = data.get("dry_run", False)

    if dry_run:
        _log_command(f"ARBITER [DRY]: {reason}")
        return json.dumps({"ok": True, "executed": False, "reason": reason})

    if action == "discharge":
        p = build_and_wrap(build_mode_command(self_powered=True))
        mqtt_handler.publish_command(p, commands_live)
        auto.manual_mode_change(2, override_minutes=10)
        _log_command(f"ARBITER: {reason}")
        if commands_live:
            ep_str = f"{price_state.effective_price:.1f}¢" if price_state.effective_price else "?"
            soc_str = f"{power_state.soc_pct:.0f}%" if power_state.soc_pct else "?"
            notifier.notify("mode_self_powered",
                f"⚡ <b>Arbiter → Self-Powered</b>\nPrice: {ep_str} | SOC: {soc_str}")

    elif action == "charge":
        rate = int(data.get("rate", 3000))
        max_soc = int(data.get("max_soc", thresholds.max_soc))
        p = build_and_wrap(build_mode_command(self_powered=False))
        mqtt_handler.publish_command(p, commands_live)
        p1 = build_and_wrap(build_charge_command(True))
        mqtt_handler.publish_command(p1, commands_live)
        p2 = build_and_wrap(build_charge_power_command(rate, max_soc))
        mqtt_handler.publish_command(p2, commands_live)
        auto.manual_mode_change(1, override_minutes=10)
        _log_command(f"ARBITER: {reason}")
        if commands_live:
            ep_str = f"{price_state.effective_price:.1f}¢" if price_state.effective_price else "?"
            notifier.notify("charge_start",
                f"🔋 <b>Arbiter → Charging</b> at {rate}W\nPrice: {ep_str}")

    elif action == "backup":
        p = build_and_wrap(build_mode_command(self_powered=False))
        mqtt_handler.publish_command(p, commands_live)
        p1 = build_and_wrap(build_charge_command(False))
        mqtt_handler.publish_command(p1, commands_live)
        auto.manual_mode_change(1, override_minutes=10)
        _log_command(f"ARBITER: {reason}")

    elif action == "hold":
        _log_command(f"ARBITER [HOLD]: {reason}")
        return json.dumps({"ok": True, "executed": False, "reason": reason})

    else:
        return json.dumps({"ok": False, "error": f"Unknown action: {action}"}), 400

    _broadcast()
    return json.dumps({"ok": True, "executed": True, "reason": reason})


# ─── WebSocket ──────────────────────────────────────────────────────────────

def _build_state_msg():
    """Serialize full dashboard state to JSON."""
    return json.dumps({
        "type":          "state",
        "power":         power_state.to_dict(),
        "price":         price_state.to_dict(),
        "thresholds":    thresholds.to_dict(),
        "auto": {
            "enabled":       auto.enabled,
            "last_decision": auto.last_decision,
            "override_remaining": max(0, int(auto.manual_override_until - time.time()))
                                  if auto.manual_override_until > time.time() else 0,
        },
        "history":       history.to_dict(),
        "commands_live":  commands_live,
        "command_log":    command_log[-30:],
        "mqtt_connected": mqtt_handler.is_alive if mqtt_handler else False,
        "telegram":       notifier.to_dict(),
        "battery_cost":   battery_pool.to_dict(),
        "energy_hour":    energy_tracker.to_dict(),
        "enphase":        enphase_state.to_dict(),
        "kia":            kia_state.to_dict(),
        "kia_auto": {
            "enabled":       kia_auto.enabled,
            "last_decision": kia_auto.last_decision,
            "override_remaining": max(0, int(kia_auto.manual_override_until - time.time()))
                                  if kia_auto.manual_override_until > time.time() else 0,
        },
    })


def _broadcast():
    """Push state to all connected WebSocket clients."""
    msg = _build_state_msg()
    dead = set()
    with _ws_lock:
        for ws in list(_ws_clients):
            try:
                ws.send(msg)
            except Exception:
                dead.add(ws)
        for d in dead:
            _ws_clients.discard(d)


def _log_command(text: str):
    global command_log
    entry = {
        "ts":   datetime.datetime.now().strftime("%H:%M:%S"),
        "live": commands_live,
        "text": text,
    }
    command_log.append(entry)
    if len(command_log) > 50:
        command_log = command_log[-30:]
    log.info("CMD [%s] %s", "LIVE" if commands_live else "DRY", text)
    # CSV logging
    logger.log_command(text, commands_live, power_state, price_state)


def _handle_command(data: dict):
    """Process a command from the frontend."""
    global commands_live, thresholds
    cmd = data.get("cmd")

    if cmd == "mode":
        val = data.get("value", "backup")
        sp = (val == "self_powered")
        override_min = data.get("override_minutes", 5)
        payload = build_and_wrap(build_mode_command(self_powered=sp))
        mqtt_handler.publish_command(payload, commands_live)
        # Tell automation about the manual mode change so it doesn't fight it
        mode_int = 2 if sp else 1
        auto.manual_mode_change(mode_int, override_minutes=int(override_min))
        _log_command(f"MODE → {'Self-Powered' if sp else 'Backup'} (override {override_min}m)")

    elif cmd == "toggle_eps":
        enable = data.get("value", False)
        payload = build_and_wrap(build_eps_command(enable))
        mqtt_handler.publish_command(payload, commands_live)
        power_state.eps_mode = enable  # optimistic local update
        _log_command(f"EPS → {'ON (20ms switchover)' if enable else 'OFF'}")

    elif cmd == "charge_start":
        rate    = int(data.get("rate", 3000))
        max_soc = int(data.get("max_soc", 95))
        # Send charge ON + power setting
        p1 = build_and_wrap(build_charge_command(True))
        mqtt_handler.publish_command(p1, commands_live)
        p2 = build_and_wrap(build_charge_power_command(rate, max_soc))
        mqtt_handler.publish_command(p2, commands_live)
        _log_command(f"CHARGE START {rate}W  SOC≤{max_soc}%")

    elif cmd == "charge_stop":
        payload = build_and_wrap(build_charge_command(False))
        mqtt_handler.publish_command(payload, commands_live)
        _log_command("CHARGE STOP")

    elif cmd == "apply_rate":
        rate    = int(data.get("rate", 3000))
        max_soc = int(data.get("max_soc", 95))
        payload = build_and_wrap(build_charge_power_command(rate, max_soc))
        mqtt_handler.publish_command(payload, commands_live)
        _log_command(f"RATE → {rate}W  SOC≤{max_soc}%")

    elif cmd == "toggle_auto":
        auto.enabled = not auto.enabled
        _log_command(f"AUTO {'ON' if auto.enabled else 'OFF'}")

    elif cmd == "toggle_live":
        commands_live = not commands_live
        _log_command(f"COMMANDS → {'LIVE' if commands_live else 'DRY RUN'}")

    elif cmd == "cancel_override":
        auto.cancel_override()
        _log_command("Manual override cancelled")

    elif cmd == "set_threshold":
        key = data.get("key")
        val = data.get("value")
        if key and val is not None and hasattr(thresholds, key):
            field_type = type(getattr(thresholds, key))
            setattr(thresholds, key, field_type(val))
            # Link max_soc ↔ high band ceiling: they are the same concept
            # (high band charges up to max_soc). Keep slider-soc in sync too.
            if key == "max_soc":
                pass  # max_soc IS the high band ceiling — nothing else to sync
            thresholds.save()
            _log_command(f"THRESHOLD {key} → {val}")
            # Trigger immediate automation re-evaluation
            if auto.enabled:
                _run_automation()

    elif cmd == "telegram_config":
        token = data.get("bot_token", "")
        chat_ids = data.get("chat_ids", [])
        events = data.get("events", {})
        notifier.configure(token, chat_ids, events)
        # Save to thresholds file
        td = thresholds.to_dict()
        notifier.save_to_thresholds(td)
        try:
            from .config import THRESHOLDS_FILE
            with open(THRESHOLDS_FILE, "w") as f:
                json.dump(td, f, indent=2)
        except Exception as e:
            log.warning("Failed to save telegram config: %s", e)
        _log_command("TELEGRAM config updated")

    elif cmd == "telegram_test":
        ok, msg = notifier.send_test()
        _log_command(f"TELEGRAM test: {msg}")

    elif cmd == "telegram_event_toggle":
        event = data.get("event")
        enabled = data.get("enabled", False)
        if event and event in notifier.events:
            notifier.events[event] = enabled
            td = thresholds.to_dict()
            notifier.save_to_thresholds(td)
            try:
                from .config import THRESHOLDS_FILE
                with open(THRESHOLDS_FILE, "w") as f:
                    json.dump(td, f, indent=2)
            except Exception as e:
                log.warning("Failed to save telegram events: %s", e)

    # ─── Kia EV commands ──────────────────────────────────────────────
    elif cmd == "kia_charge_start":
        if kia_poller:
            ok, msg = kia_poller.start_charge()
            _log_command(f"KIA CHARGE START: {msg}")
            kia_auto.manual_override(minutes=int(data.get("override_minutes", 15)))

    elif cmd == "kia_charge_stop":
        if kia_poller:
            ok, msg = kia_poller.stop_charge()
            _log_command(f"KIA CHARGE STOP: {msg}")
            kia_auto.manual_override(minutes=int(data.get("override_minutes", 15)))

    elif cmd == "kia_set_limits":
        if kia_poller:
            ac = int(data.get("ac_limit", 90))
            dc = int(data.get("dc_limit", 80))
            ok, msg = kia_poller.set_charge_limits(ac, dc)
            _log_command(f"KIA LIMITS: {msg}")

    elif cmd == "kia_toggle_auto":
        kia_auto.enabled = not kia_auto.enabled
        _log_command(f"KIA AUTO {'ON' if kia_auto.enabled else 'OFF'}")

    elif cmd == "kia_cancel_override":
        kia_auto.cancel_override()
        _log_command("KIA override cancelled")

    elif cmd == "kia_refresh":
        if kia_poller:
            kia_poller.force_refresh()
            _log_command("KIA force refresh")

    _broadcast()


@sock.route("/ws")
def websocket(ws):
    with _ws_lock:
        _ws_clients.add(ws)
    log.info("WebSocket client connected (%d total)", len(_ws_clients))
    try:
        # Send current state immediately
        ws.send(_build_state_msg())
        while True:
            raw = ws.receive(timeout=60)
            if raw is None:
                # keepalive — send state
                ws.send(_build_state_msg())
                continue
            try:
                data = json.loads(raw)
                _handle_command(data)
            except json.JSONDecodeError:
                log.warning("Invalid JSON from WebSocket: %s", raw[:100])
    except Exception:
        pass
    finally:
        with _ws_lock:
            _ws_clients.discard(ws)
        log.info("WebSocket client disconnected (%d remaining)", len(_ws_clients))


# ─── Automation ─────────────────────────────────────────────────────────────

def _run_automation():
    """Evaluate automation decision and execute if appropriate."""
    mode, rate, reason = auto.decide(price_state, power_state, thresholds)

    ok, why = auto.should_send(mode, rate)
    if not ok:
        # Show the decision + why it's blocked
        if why == "no change":
            auto.last_decision = reason or auto.last_decision
        elif "manual override" in why:
            auto.last_decision = f"OVERRIDE: {reason} (paused — {why})"
        else:
            auto.last_decision = reason or auto.last_decision
        return

    auto.last_decision = reason or auto.last_decision

    # Execute
    prev_mode = auto.last_mode
    if mode == 2:
        # Self-Powered (discharge)
        p = build_and_wrap(build_mode_command(self_powered=True))
        mqtt_handler.publish_command(p, commands_live)
        _log_command(f"AUTO: {reason}")
        # Notify: mode → Self-Powered
        if prev_mode != 2 and commands_live:
            ep_str = f"{price_state.effective_price:.1f}¢" if price_state.effective_price else "?"
            soc_str = f"{power_state.soc_pct:.0f}%" if power_state.soc_pct else "?"
            notifier.notify("mode_self_powered",
                f"⚡ <b>Self-Powered</b> — discharging battery\nPrice: {ep_str} | SOC: {soc_str}")
    elif mode == 1:
        # Backup mode
        p = build_and_wrap(build_mode_command(self_powered=False))
        mqtt_handler.publish_command(p, commands_live)
        if rate and rate > 0:
            # Start charging at specified rate
            p1 = build_and_wrap(build_charge_command(True))
            mqtt_handler.publish_command(p1, commands_live)
            p2 = build_and_wrap(build_charge_power_command(rate, int(thresholds.max_soc)))
            mqtt_handler.publish_command(p2, commands_live)
            _log_command(f"AUTO: {reason}")
            # Notify: charge started
            if commands_live:
                ep_str = f"{price_state.effective_price:.1f}¢" if price_state.effective_price else "?"
                notifier.notify("charge_start",
                    f"🔋 <b>Charging</b> at {rate}W\nPrice: {ep_str}")
        elif rate == 0:
            # Stop charging
            p1 = build_and_wrap(build_charge_command(False))
            mqtt_handler.publish_command(p1, commands_live)
            _log_command(f"AUTO: {reason}")
            # Notify: back to backup
            if prev_mode == 2 and commands_live:
                ep_str = f"{price_state.effective_price:.1f}¢" if price_state.effective_price else "?"
                soc_str = f"{power_state.soc_pct:.0f}%" if power_state.soc_pct else "?"
                notifier.notify("mode_backup",
                    f"🔌 <b>Backup mode</b> — grid powering home\nPrice: {ep_str} | SOC: {soc_str}")

    auto.record(mode, rate, reason)

    # Outage detection notification
    if reason and "OUTAGE:" in reason and commands_live:
        notifier.notify("grid_outage",
            "🚨 <b>Grid Outage Detected</b>\nBattery is powering home. Grid appears down.")


# ─── Kia Automation ────────────────────────────────────────────────────────

def _run_kia_automation():
    """Evaluate Kia charge decision and execute if appropriate."""
    if not kia_poller or not kia_state.available:
        return

    action, ac_limit, reason = kia_auto.decide(price_state, kia_state, thresholds)

    ok, why = kia_auto.should_send(action, ac_limit)
    if not ok:
        if why == "no change":
            kia_auto.last_decision = reason or kia_auto.last_decision
        elif "manual override" in why:
            kia_auto.last_decision = f"OVERRIDE: {reason} (paused \u2014 {why})"
        else:
            kia_auto.last_decision = reason or kia_auto.last_decision
        return

    kia_auto.last_decision = reason or kia_auto.last_decision

    if action == "charge":
        # Set AC limit first, then start charging
        if ac_limit and ac_limit != kia_auto.last_ac_limit:
            dc = getattr(thresholds, "kia_dc_limit", 80)
            kia_poller.set_charge_limits(ac_limit, dc)
        if kia_auto.last_action != "charge":
            success, msg = kia_poller.start_charge()
            if success and commands_live:
                _log_command(f"KIA AUTO: {reason}")
                ep_str = f"{price_state.effective_price:.1f}\u00a2" if price_state.effective_price else "?"
                notifier.notify("kia_charge_start",
                    f"\U0001F697 <b>EV Charging</b> to {ac_limit}%\nPrice: {ep_str}")
        else:
            _log_command(f"KIA AUTO: {reason}")
    elif action == "stop":
        if kia_auto.last_action != "stop":
            success, msg = kia_poller.stop_charge()
            if success and commands_live:
                _log_command(f"KIA AUTO: {reason}")
                ep_str = f"{price_state.effective_price:.1f}\u00a2" if price_state.effective_price else "?"
                notifier.notify("kia_charge_stop",
                    f"\U0001F6D1 <b>EV Charge Stopped</b>\nPrice: {ep_str}")
        else:
            _log_command(f"KIA AUTO: {reason}")

    kia_auto.record(action, ac_limit, reason)


def _on_kia_update():
    """Called from Kia poller thread when vehicle status updates."""
    _broadcast()
    if kia_auto.enabled:
        _run_kia_automation()


# ─── Enphase callback ──────────────────────────────────────────────────────

def _on_enphase_update():
    """Called from Enphase poller thread when new solar data arrives."""
    _broadcast()


# ─── Callbacks ──────────────────────────────────────────────────────────────

def _on_telemetry_update():
    """Called from MQTT thread when new telemetry arrives."""
    global _pool_initialized

    # Update battery cost pool and energy tracker
    ep = price_state.effective_price
    bw = power_state.battery_w or 0.0
    soc = power_state.soc_pct

    if ep is not None:
        # Initialize battery pool from SOC on first telemetry with valid SOC
        if not _pool_initialized and soc is not None and soc > 0:
            if battery_pool.total_wh < 1:
                battery_pool.initialize_from_soc(soc)
            _pool_initialized = True

        # Battery pool uses total cost (supply + T&D) since that's the real
        # cost of energy going into the battery
        total_cost = ep + (thresholds.td_rate_cents or 0)
        battery_pool.update(bw, total_cost, soc)
        energy_tracker.update(
            power_state.grid_w or 0.0,
            power_state.load_w or 0.0,
            bw, total_cost,
        )

    _broadcast()
    if auto.enabled:
        _run_automation()


def _on_price_update():
    """Called from ComEd poller thread when new prices arrive."""
    logger.log_price(price_state)
    # Price spike notification
    if price_state.effective_price is not None and price_state.effective_price >= 14.0:
        notifier.notify("price_spike",
            f"📈 <b>Price Spike!</b> {price_state.effective_price:.1f}¢/kWh")
    _broadcast()
    if auto.enabled:
        _run_automation()
    if kia_auto.enabled:
        _run_kia_automation()


# ─── Periodic tick (30s automation re-eval) ─────────────────────────────────

def _save_runtime_state():
    """Persist battery pool and energy tracker to JSON."""
    state = {
        "battery_pool": battery_pool.save_state(),
        "energy_hour": energy_tracker.save_state(),
    }
    try:
        with _state_lock:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
    except Exception as e:
        log.warning("Failed to save runtime state: %s", e)


def _load_runtime_state():
    """Restore battery pool and energy tracker from JSON."""
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        battery_pool.load_state(state.get("battery_pool"))
        energy_tracker.load_state(state.get("energy_hour"))
    except Exception as e:
        log.warning("Failed to load runtime state: %s", e)


_tick_count = 0

def _tick_loop():
    """Independent 30s timer for automation re-evaluation + state persistence."""
    global _tick_count
    while True:
        time.sleep(30)
        _tick_count += 1
        if auto.enabled:
            _run_automation()
        if kia_auto.enabled:
            _run_kia_automation()
        _broadcast()
        # Save runtime state every ~60s (every 2 ticks)
        if _tick_count % 2 == 0:
            _save_runtime_state()


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    global mqtt_handler, kia_poller

    log.info("Starting EcoFlow Web Dashboard...")

    # Load persisted runtime state (battery pool, energy tracker)
    _load_runtime_state()

    # Start MQTT
    mqtt_handler = MQTTHandler(power_state, history, _on_telemetry_update)
    mqtt_handler.start()
    log.info("MQTT handler started")

    # Start ComEd poller
    comed = ComedPoller(price_state, _on_price_update)
    comed.start()
    log.info("ComEd poller started")

    # Start Enphase poller (if credentials exist)
    if os.path.exists(ENPHASE_CREDENTIALS_FILE):
        enphase_poller = EnphasePoller(enphase_state, _on_enphase_update)
        enphase_poller.start()
        log.info("Enphase poller started")
    else:
        log.info("Enphase credentials not found — solar features disabled")

    # Start Kia poller (if credentials exist)
    if os.path.exists(KIA_CREDENTIALS_FILE):
        kia_poller = KiaPoller(kia_state, _on_kia_update)
        kia_poller.start()
    else:
        log.info("Kia credentials not found — EV features disabled")

    # Start periodic automation tick
    threading.Thread(target=_tick_loop, daemon=True).start()
    log.info("Automation tick started (30s interval)")

    # Start Flask
    log.info("Web dashboard at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
