from service_hub.webhooks.keys import delivery_key


def dispatch_delivery(store, delivery, handler):
    """Dispatch a webhook once."""
    key = delivery_key(delivery)
    if store.has_seen(key):
        return "duplicate"
    store.mark_seen(key)
    handler(delivery["payload"])
    return "processed"
