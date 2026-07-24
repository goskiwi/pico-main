from service_hub.webhooks.service import dispatch_delivery


def handle_event(store, delivery, handler):
    """Process one serialized webhook delivery."""
    return dispatch_delivery(store, delivery, handler)
