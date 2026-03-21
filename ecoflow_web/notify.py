"""
Telegram notification system for EcoFlow dashboard.

Setup:
1. Message @BotFather on Telegram → /newbot → get bot token
2. Message your bot, then visit https://api.telegram.org/bot<TOKEN>/getUpdates
   to find your chat_id
3. Enter token + chat_id in the Controls tab of the dashboard
"""

import json
import logging
import threading
import time
import urllib.request
import urllib.error

log = logging.getLogger("ecoflow")

# Rate limiting: max 1 message per event type per 5 minutes
_RATE_LIMIT_SECS = 300
_last_sent = {}  # event_type → timestamp
_lock = threading.Lock()


class TelegramNotifier:
    """Sends Telegram messages via Bot API. Thread-safe."""

    def __init__(self):
        self.bot_token = ""
        self.chat_ids = []  # list of chat_id strings
        self.events = {
            "mode_self_powered": True,
            "mode_backup": True,
            "charge_start": False,
            "charge_stop": False,
            "grid_outage": True,
            "price_spike": True,
            "kia_charge_start": False,
            "kia_charge_stop": False,
        }

    def configure(self, bot_token: str, chat_ids: list, events: dict = None):
        self.bot_token = bot_token.strip()
        self.chat_ids = [str(c).strip() for c in chat_ids if str(c).strip()]
        if events:
            self.events.update(events)

    @property
    def is_configured(self):
        return bool(self.bot_token and self.chat_ids)

    def to_dict(self):
        return {
            "bot_token": self.bot_token,
            "chat_ids": self.chat_ids,
            "events": self.events,
        }

    def load_from_thresholds(self, data: dict):
        """Load config from thresholds JSON dict."""
        self.bot_token = data.get("telegram_bot_token", "")
        self.chat_ids = data.get("telegram_chat_ids", [])
        evts = data.get("telegram_events", {})
        if evts:
            self.events.update(evts)

    def save_to_thresholds(self, data: dict):
        """Write config into thresholds dict for persistence."""
        data["telegram_bot_token"] = self.bot_token
        data["telegram_chat_ids"] = self.chat_ids
        data["telegram_events"] = self.events

    def notify(self, event_type: str, message: str):
        """Send notification if event is enabled and not rate-limited."""
        if not self.is_configured:
            return
        if not self.events.get(event_type, False):
            return

        now = time.time()
        with _lock:
            last = _last_sent.get(event_type, 0)
            if now - last < _RATE_LIMIT_SECS:
                log.debug("Telegram rate-limited: %s (%.0fs ago)", event_type, now - last)
                return
            _last_sent[event_type] = now

        # Send in background thread to not block automation
        threading.Thread(
            target=self._send_all,
            args=(message,),
            daemon=True,
        ).start()

    def send_test(self):
        """Send a test message to all configured chat IDs. Returns (ok, error)."""
        if not self.bot_token:
            return False, "No bot token configured"
        if not self.chat_ids:
            return False, "No chat IDs configured"
        try:
            self._send_all("🔋 EcoFlow Dashboard test notification — connection OK!")
            return True, "Test message sent"
        except Exception as e:
            return False, str(e)

    def _send_all(self, text: str):
        """Send message to all configured chat IDs."""
        for chat_id in self.chat_ids:
            try:
                self._send_message(chat_id, text)
            except Exception as e:
                log.warning("Telegram send failed (chat %s): %s", chat_id, e)

    def _send_message(self, chat_id: str, text: str):
        """Send a single Telegram message."""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                if result.get("ok"):
                    log.info("Telegram sent to %s: %s", chat_id, text[:60])
                else:
                    log.warning("Telegram API error: %s", result)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            log.warning("Telegram HTTP %d: %s", e.code, body[:200])
            raise
