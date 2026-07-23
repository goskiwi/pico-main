def delivery_key(delivery):
    """Return the identity used for webhook deduplication."""
    return delivery["event_id"]
