from service_hub.notifications.service import render_notification


def render_receipt(event, templates):
    """Render a receipt event with a locale-specific template."""
    return render_notification(
        event["locale"],
        event["values"],
        templates,
    )
