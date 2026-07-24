def handle_event(store, delivery, handler):
    """Legacy best-effort handler; not part of the active webhook path."""
    handler(delivery["payload"])
    return "accepted"


def delivery_key(delivery):
    return f"legacy:{delivery['event_id']}"
