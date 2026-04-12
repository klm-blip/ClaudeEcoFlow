"""
MQTT client: connects to EcoFlow broker, subscribes to telemetry, publishes commands.
"""

import json
import logging
import time

import paho.mqtt.client as mqtt

from .config import (
    MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS, CLIENT_ID,
    SESSION_ID, GATEWAY_SN, INVERTER_SN, TELEMETRY_TOPICS, COMMAND_TOPIC,
)

SET_REPLY_TOPIC = f"/app/{SESSION_ID}/{GATEWAY_SN}/thing/property/set_reply"
from .state import PowerState, parse_payload
from .history import HistoryBuffer

log = logging.getLogger("ecoflow")


class MQTTHandler:
    def __init__(self, state: PowerState, history: HistoryBuffer, on_update):
        self.state       = state
        self.history     = history
        self.on_update   = on_update
        self.connected   = False
        self.last_msg_ts = 0.0
        self.last_cmd_ts = 0.0  # when we last published a command
        self.pending_ack = False  # waiting for set_reply?
        self.publish_ok_count  = 0
        self.publish_fail_count = 0
        self.reconnect_count = 0
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION1,
            client_id=CLIENT_ID,
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
        self._client.username_pw_set(MQTT_USER, MQTT_PASS)
        self._client.tls_set()
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        self.connected = (rc == 0)
        if rc == 0:
            log.info("MQTT connected OK")
            for t in TELEMETRY_TOPICS:
                client.subscribe(t, qos=1)
            client.subscribe(SET_REPLY_TOPIC, qos=1)
            log.info("Subscribed to set_reply: %s", SET_REPLY_TOPIC)
            self._request_quotas(client)
        else:
            log.error("MQTT connect failed rc=%d", rc)

    def _request_quotas(self, client):
        """Send latestQuotas GET to trigger telemetry from both devices."""
        for sn in (GATEWAY_SN, INVERTER_SN):
            get_topic = f"/app/{SESSION_ID}/{sn}/thing/property/get"
            msg = json.dumps({
                "from": "Android",
                "id": str(int(time.time() * 1000)),
                "moduleSn": sn,
                "moduleType": 0,
                "operateType": "latestQuotas",
                "params": {},
                "version": "1.0",
                "lang": "en-us",
            })
            client.publish(get_topic, msg.encode(), qos=1)
            log.info("Sent latestQuotas GET to %s", sn)

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            log.warning("MQTT disconnected rc=%d, will auto-reconnect", rc)

    @property
    def is_alive(self):
        """More robust connection check: connected flag OR received data recently."""
        if self.connected:
            return True
        # If we got a message within the last 90s, the connection is probably
        # just briefly cycling — treat as alive to avoid UI flicker.
        if self.last_msg_ts > 0 and (time.time() - self.last_msg_ts) < 90:
            return True
        return False

    def _on_message(self, client, userdata, msg):
        self.last_msg_ts = time.time()
        # Handle set_reply (ACK/NAK for commands)
        if msg.topic == SET_REPLY_TOPIC:
            self.pending_ack = False
            try:
                # Try to decode as protobuf or JSON
                try:
                    reply = json.loads(msg.payload)
                    log.info("CMD ACK (JSON): %s", reply)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    log.info("CMD ACK (proto): %d bytes", len(msg.payload))
            except Exception as e:
                log.debug("set_reply parse error: %s", e)
            return
        if parse_payload(msg.payload, self.state):
            self.history.maybe_add(self.state)
            self.on_update()

    def start(self):
        self._client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=120)
        self._client.loop_start()

    def reconnect(self):
        """Force a clean MQTT reconnect — used to recover from publish-zombie state."""
        self.reconnect_count += 1
        log.warning("MQTT: forcing reconnect (#%d)", self.reconnect_count)
        try:
            self._client.disconnect()
        except Exception as e:
            log.warning("MQTT disconnect error: %s", e)
        try:
            self._client.reconnect()
        except Exception as e:
            log.error("MQTT reconnect error: %s", e)

    def publish_command(self, payload: bytes, commands_live: bool = False) -> bool:
        """Send a protobuf-encoded command. Returns True if broker ACKed (QoS 1)."""
        if not commands_live:
            log.info("CMD DRY  %d bytes: %s", len(payload), payload.hex())
            return True

        self.pending_ack = True
        self.last_cmd_ts = time.time()
        info = self._client.publish(COMMAND_TOPIC, payload, qos=1)

        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            self.publish_fail_count += 1
            log.error("CMD PUBLISH FAILED rc=%s — forcing reconnect", info.rc)
            self.pending_ack = False
            self.reconnect()
            return False

        # Wait for broker PUBACK (QoS 1 confirmation that broker received it)
        try:
            info.wait_for_publish(timeout=10)
            self.publish_ok_count += 1
            log.info("CMD LIVE (ACKed)  %d bytes: %s", len(payload), payload.hex())
            return True
        except ValueError:
            self.publish_fail_count += 1
            log.error("CMD PUBLISH invalid state — forcing reconnect")
            self.pending_ack = False
            self.reconnect()
            return False
        except RuntimeError:
            self.publish_fail_count += 1
            log.error("CMD PUBLISH not queued — forcing reconnect")
            self.pending_ack = False
            self.reconnect()
            return False
        except Exception:
            self.publish_fail_count += 1
            log.error("CMD PUBLISH timeout (no PUBACK in 10s) — forcing reconnect")
            self.pending_ack = False
            self.reconnect()
            return False
