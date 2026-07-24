from service_hub.notifications.api import render_receipt


def test_exact_locale_template_is_rendered():
    event = {"locale": "en-US", "values": {"total": "$12"}}
    templates = {"en-US": "Total: {total}", "default": "Amount: {total}"}
    assert render_receipt(event, templates) == "Total: $12"
