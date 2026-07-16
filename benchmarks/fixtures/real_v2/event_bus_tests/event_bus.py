class EventBus:
    def __init__(self):
        self._listeners = {}

    def subscribe(self, event, callback):
        self._listeners.setdefault(event, []).append(callback)

    def unsubscribe(self, event, callback):
        listeners = self._listeners.get(event, [])
        if callback not in listeners:
            raise ValueError("callback is not subscribed")
        listeners.remove(callback)

    def publish(self, event, payload):
        return [callback(payload) for callback in tuple(self._listeners.get(event, []))]
