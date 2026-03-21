"""
Kia Connect API wrapper: auth with token persistence, vehicle polling, charge control.
"""

import json
import logging
import os
import threading
import time

from .config import KIA_CREDENTIALS_FILE, KIA_TOKEN_FILE, KIA_POLL_SECONDS
from .state import KiaState

log = logging.getLogger("ecoflow")

# Lazy import — only load the heavy library when actually needed
_VehicleManager = None
_Token = None
_OTP_NOTIFY_TYPE = None


def _ensure_imports():
    global _VehicleManager, _Token, _OTP_NOTIFY_TYPE
    if _VehicleManager is None:
        from hyundai_kia_connect_api import VehicleManager, Token
        from hyundai_kia_connect_api.const import OTP_NOTIFY_TYPE
        _VehicleManager = VehicleManager
        _Token = Token
        _OTP_NOTIFY_TYPE = OTP_NOTIFY_TYPE


def _load_kia_credentials():
    """Load Kia Connect credentials from file. Returns dict or None."""
    if not os.path.exists(KIA_CREDENTIALS_FILE):
        return None
    creds = {}
    with open(KIA_CREDENTIALS_FILE) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
    return creds if creds.get("KIA_EMAIL") and creds.get("KIA_PASSWORD") else None


class KiaPoller:
    """Polls Kia Connect API in a background thread, manages auth + token persistence."""

    def __init__(self, kia_state: KiaState, on_update):
        self.ks = kia_state
        self.on_update = on_update
        self._stop = threading.Event()
        self._vm = None         # VehicleManager instance
        self._vid = None        # vehicle ID
        self._creds = None
        self._lock = threading.Lock()

    def start(self):
        self._creds = _load_kia_credentials()
        if not self._creds:
            log.warning("Kia credentials not found — EV features disabled")
            return
        self.ks.available = True
        threading.Thread(target=self._loop, daemon=True).start()
        log.info("Kia poller started (interval %ds)", KIA_POLL_SECONDS)

    def _loop(self):
        # Initial auth + poll
        self._authenticate()
        if self._vm and self._vid:
            self._poll()
        while not self._stop.wait(KIA_POLL_SECONDS):
            if not self._vm:
                self._authenticate()
            if self._vm:
                self._poll()

    def _authenticate(self):
        """Authenticate with Kia Connect, using saved token if available."""
        _ensure_imports()
        try:
            email = self._creds["KIA_EMAIL"]
            password = self._creds["KIA_PASSWORD"]
            pin = self._creds.get("KIA_PIN", "")

            vm = _VehicleManager(
                region=3,       # USA
                brand=1,        # Kia
                username=email,
                password=password,
                pin=pin,
            )

            # Try saved token (rmtoken reuse — skips OTP)
            if os.path.exists(KIA_TOKEN_FILE):
                with open(KIA_TOKEN_FILE) as f:
                    token_data = json.load(f)
                saved_token = _Token.from_dict(token_data)
                result = vm.api.login(email, password, saved_token)

                if isinstance(result, _Token):
                    vm.token = result
                    if not vm.token.refresh_token and saved_token.refresh_token:
                        vm.token.refresh_token = saved_token.refresh_token
                    vm.initialize_vehicles()
                    self._save_token(vm.token)
                    log.info("Kia: logged in using saved rmtoken")
                else:
                    log.warning("Kia: saved token rejected, OTP needed. "
                                "Run test_kia_charge.py manually to re-authenticate.")
                    self.ks.error = "Token expired — run test_kia_charge.py for OTP"
                    return
            else:
                # No saved token — need OTP (can't do interactively in daemon)
                log.warning("Kia: no saved token. Run test_kia_charge.py to authenticate first.")
                self.ks.error = "No token — run test_kia_charge.py to authenticate"
                return

            # Discover vehicle
            if not vm.vehicles:
                log.warning("Kia: no vehicles found")
                self.ks.error = "No vehicles found"
                return

            vid = list(vm.vehicles.keys())[0]
            vehicle = vm.vehicles[vid]

            with self._lock:
                self._vm = vm
                self._vid = vid
                self.ks.vehicle_name = vehicle.name or "Kia EV"
                self.ks.vehicle_id = vid
                self.ks.error = ""

            log.info("Kia: authenticated — vehicle '%s' (VID %s)", vehicle.name, vid)

        except Exception as e:
            log.warning("Kia auth failed: %s", e)
            self.ks.error = str(e)

    def _poll(self):
        """Fetch cached vehicle state from Kia servers."""
        try:
            with self._lock:
                vm, vid = self._vm, self._vid
            if not vm or not vid:
                return

            vm.update_all_vehicles_with_cached_state()
            vehicle = vm.vehicles[vid]

            self.ks.soc_pct = vehicle.ev_battery_percentage
            self.ks.charging = bool(vehicle.ev_battery_is_charging)
            self.ks.plugged_in = bool(vehicle.ev_battery_is_plugged_in)
            self.ks.range_miles = vehicle.ev_driving_range
            self.ks.odometer = vehicle.odometer
            self.ks.last_update = time.time()
            self.ks.error = ""

            log.info("Kia: SOC=%s%% charging=%s plugged=%s range=%s mi",
                     self.ks.soc_pct, self.ks.charging, self.ks.plugged_in, self.ks.range_miles)
            self.on_update()

        except Exception as e:
            log.warning("Kia poll failed: %s", e)
            self.ks.error = str(e)
            # Token may have expired — clear VM to trigger re-auth next cycle
            if "Missing payload" in str(e) or "session" in str(e).lower():
                with self._lock:
                    self._vm = None

    def _save_token(self, token):
        """Persist token to JSON for rmtoken reuse."""
        try:
            with open(KIA_TOKEN_FILE, "w") as f:
                json.dump(token.to_dict(), f, indent=2)
        except Exception as e:
            log.warning("Failed to save Kia token: %s", e)

    # ─── Commands ─────────────────────────────────────────────────────────

    def start_charge(self):
        """Start EV charging. Returns (success, message)."""
        with self._lock:
            vm, vid = self._vm, self._vid
        if not vm or not vid:
            return False, "Kia not connected"
        try:
            vm.start_charge(vid)
            self._save_token(vm.token)
            log.info("Kia: start_charge sent")
            return True, "Charge started"
        except Exception as e:
            log.warning("Kia start_charge failed: %s", e)
            return False, str(e)

    def stop_charge(self):
        """Stop EV charging. Returns (success, message)."""
        with self._lock:
            vm, vid = self._vm, self._vid
        if not vm or not vid:
            return False, "Kia not connected"
        try:
            vm.stop_charge(vid)
            self._save_token(vm.token)
            log.info("Kia: stop_charge sent")
            return True, "Charge stopped"
        except Exception as e:
            log.warning("Kia stop_charge failed: %s", e)
            return False, str(e)

    def set_charge_limits(self, ac_limit: int, dc_limit: int):
        """Set AC/DC charge limits (percent). Returns (success, message)."""
        with self._lock:
            vm, vid = self._vm, self._vid
        if not vm or not vid:
            return False, "Kia not connected"
        try:
            vm.set_charge_limits(vid, ac_limit, dc_limit)
            self._save_token(vm.token)
            self.ks.ac_charge_limit = ac_limit
            self.ks.dc_charge_limit = dc_limit
            log.info("Kia: charge limits set AC=%d%% DC=%d%%", ac_limit, dc_limit)
            return True, f"Limits set: AC {ac_limit}% DC {dc_limit}%"
        except Exception as e:
            log.warning("Kia set_charge_limits failed: %s", e)
            return False, str(e)

    def force_refresh(self):
        """Force a fresh poll (not cached). Uses extra API calls."""
        with self._lock:
            vm, vid = self._vm, self._vid
        if not vm or not vid:
            return
        try:
            vm.force_refresh_all_vehicles_states()
            vehicle = vm.vehicles[vid]
            self.ks.soc_pct = vehicle.ev_battery_percentage
            self.ks.charging = bool(vehicle.ev_battery_is_charging)
            self.ks.plugged_in = bool(vehicle.ev_battery_is_plugged_in)
            self.ks.range_miles = vehicle.ev_driving_range
            self.ks.last_update = time.time()
            self.ks.error = ""
            self.on_update()
        except Exception as e:
            log.warning("Kia force refresh failed: %s", e)
