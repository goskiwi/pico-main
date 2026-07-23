def template_for(templates, locale):
    """Legacy default-only lookup."""
    return templates["default"]


def render_receipt(event, templates):
    return template_for(templates, event["locale"]).format(**event["values"])
