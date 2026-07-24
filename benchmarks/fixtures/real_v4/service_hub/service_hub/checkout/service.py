from service_hub.checkout.models import line_total


def merchandise_total(items):
    """Return the merchandise subtotal for serialized line items."""
    return sum(line_total(item) for item in items)
