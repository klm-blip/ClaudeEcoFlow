"""
Enphase Envoy local API poller — reads solar production and consumption.

Uses pyenphase for auth (cloud JWT) and local HTTPS polling.
Runs async code in a dedicated thread with its own event loop.
"""

import asyncio
import logging
import os
import threading
import time

from .config import ENPHASE_CREDENTIALS_FILE, ENPHASE_POLL_SECONDS

log = logging.getLogger("ecoflow")


def _load_enphase_credentials():
    """Load Enphase credentials from file."""
    creds = {"ENPHASE_EMAIL": "", "ENPHASE_PASSWORD": "", "ENPHASE_HOST": ""}
    if not os.path.exists(ENPHASE_CREDENTIALS_FILE):
        return creds
    for line in open(ENPHASE_CREDENTIALS_FILE).read().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k in creds:
                creds[k] = v
    return creds


class EnphasePoller:
    """Background poller for Enphase Envoy local API."""

    def __init__(self, state, on_update=None):
        self.state = state
        self._on_update = on_update
        self._thread = None
        self._loop = None
        self._envoy = None
        self._creds = _load_enphase_credentials()
        self._today_prod_base = 0.0
        self._today_cons_base = 0.0

        if self._creds["ENPHASE_EMAIL"] and self._creds["ENPHASE_HOST"]:
            self.state.available = True

    def start(self):
        if not self.state.available:
            log.info("Enphase credentials incomplete — solar features disabled")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        """Run async event loop in background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._poll_loop())

    async def _poll_loop(self):
        from pyenphase import Envoy

        host = self._creds["ENPHASE_HOST"]
        email = self._creds["ENPHASE_EMAIL"]
        password = self._creds["ENPHASE_PASSWORD"]

        log.info("Enphase: connecting to Envoy at %s", host)

        backoff = 30  # seconds between auth retries, increases on failure
        while True:
            # ── Connect and authenticate ──────────────────────────────
            try:
                self._envoy = Envoy(host)
                await self._envoy.setup()
                await self._envoy.authenticate(username=email, password=password)
                log.info("Enphase: authenticated successfully")
                self.state.error = ""
                backoff = 30  # reset on success
            except Exception as e:
                log.error("Enphase: auth failed: %s (retry in %ds)", e, backoff)
                self.state.error = str(e)
                if self._on_update:
                    self._on_update()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 600)  # cap at 10 minutes
                continue

            # ── Poll loop ─────────────────────────────────────────────
            consecutive_errors = 0
            while True:
                try:
                    data = await self._envoy.update()
                    self._process_data(data)
                    self.state.error = ""
                    consecutive_errors = 0
                except Exception as e:
                    consecutive_errors += 1
                    log.warning("Enphase: poll error (%d): %s", consecutive_errors, e)
                    self.state.error = str(e)

                    if consecutive_errors >= 5:
                        log.error("Enphase: %d consecutive errors, re-authenticating", consecutive_errors)
                        break  # break inner loop → re-auth in outer loop

                    # Try to re-auth in place for transient errors
                    try:
                        await self._envoy.setup()
                        await self._envoy.authenticate(username=email, password=password)
                        log.info("Enphase: re-authenticated after error")
                    except Exception as e2:
                        log.error("Enphase: re-auth failed: %s", e2)

                if self._on_update:
                    self._on_update()

                await asyncio.sleep(ENPHASE_POLL_SECONDS)

    def _process_data(self, data):
        """Extract production/consumption from pyenphase data."""
        if data.system_production is not None:
            self.state.production_w = data.system_production.watts_now
            if data.system_production.watt_hours_today is not None:
                self.state.today_production_wh = data.system_production.watt_hours_today

        if data.system_consumption is not None:
            self.state.consumption_w = data.system_consumption.watts_now
            if data.system_consumption.watt_hours_today is not None:
                self.state.today_consumption_wh = data.system_consumption.watt_hours_today

        # Net grid = consumption - production (positive = importing, negative = exporting)
        if self.state.production_w is not None and self.state.consumption_w is not None:
            self.state.net_grid_w = self.state.consumption_w - self.state.production_w

        self.state.last_update = time.time()
