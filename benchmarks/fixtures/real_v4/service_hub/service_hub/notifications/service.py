from service_hub.notifications.locale import template_for


def render_notification(locale, values, templates):
    """Resolve and format one notification."""
    template = template_for(templates, locale)
    return template.format(**values)
