def handle_event(store, delivery, handler):
    """Experimental routing hook, unused by webhook delivery."""
    handler({"route": "shadow", **delivery["payload"]})
    return "shadowed"


def render_receipt(event, templates):
    return templates["experiment"].format(**event["values"])
