def line_total(item):
    """Return one line item's total in cents."""
    return item["unit_cents"] * item["quantity"]
