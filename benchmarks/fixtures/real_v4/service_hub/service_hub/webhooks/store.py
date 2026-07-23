class DeliveryStore:
    """In-memory record of successfully handled delivery identities."""

    def __init__(self):
        self._seen = set()

    def has_seen(self, key):
        return key in self._seen

    def mark_seen(self, key):
        self._seen.add(key)

    def snapshot(self):
        return frozenset(self._seen)
