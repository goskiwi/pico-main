def quote_invoice(catalog, rules, customer, sku, quantity):
    """Deprecated first-rule pricing."""
    return catalog[sku] * quantity
