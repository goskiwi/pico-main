import time


class TTLCache:
    def __init__(self, ttl_seconds, *, clock=None):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.ttl_seconds = ttl_seconds
        self.clock = clock or time.monotonic
        self._values = {}

    def set(self, key, value):
        self._values[key] = value

    def get(self, key):
        return self._values[key]
