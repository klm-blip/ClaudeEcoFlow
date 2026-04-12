"""
Circular time-series buffer for power history (15-min rolling window).
"""

import time
from collections import deque

from .config import HISTORY_POINTS
from .state import PowerState


class HistoryBuffer:
    def __init__(self, maxlen=HISTORY_POINTS):
        self.times   = deque(maxlen=maxlen)
        self.grid    = deque(maxlen=maxlen)
        self.load    = deque(maxlen=maxlen)
        self.battery = deque(maxlen=maxlen)
        self._last   = 0.0

    def maybe_add(self, state: PowerState):
        now = time.time()
        if now - self._last < 5.0:
            return
        self._last = now
        self.times.append(now)
        self.grid.append(state.grid_w or 0)
        self.load.append(state.load_w or 0)
        self.battery.append(state.battery_w or 0)

    def to_dict(self):
        return {
            "times":   list(self.times),
            "grid":    list(self.grid),
            "load":    list(self.load),
            "battery": list(self.battery),
        }

    def save_state(self):
        return self.to_dict()

    def load_state(self, data):
        if not data:
            return
        maxlen = self.times.maxlen
        for key in ("times", "grid", "load", "battery"):
            vals = data.get(key, [])
            # Only keep the tail if saved data exceeds current maxlen
            if len(vals) > maxlen:
                vals = vals[-maxlen:]
            getattr(self, key).extend(vals)
        if self.times:
            self._last = self.times[-1]
