class LRUCache:
    def __init__(self, capacity):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._values = {}
        self._order = []

    def get(self, key):
        value = self._values[key]
        self._order.remove(key)
        self._order.append(key)
        return value

    def put(self, key, value):
        if key in self._values:
            self._values[key] = value
            return
        if len(self._values) == self.capacity:
            oldest = self._order.pop(0)
            del self._values[oldest]
        self._values[key] = value
        self._order.append(key)

    def __len__(self):
        return len(self._values)
