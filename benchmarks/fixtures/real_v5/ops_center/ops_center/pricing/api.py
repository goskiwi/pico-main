from ops_center.pricing.service import quote


def quote_invoice(catalog, rules, customer, sku, quantity):
    """Build an immutable invoice quote."""
    return quote(catalog, rules, customer, sku, quantity)
