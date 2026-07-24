def _eligible(rule, customer, sku, quantity):
    sku_matches = rule.get("sku", "*") in {"*", sku}
    quantity_matches = quantity >= int(rule.get("min_qty", 1))
    required_segment = rule.get("segment")
    segment_matches = (
        required_segment is None
        or required_segment == customer.get("segment")
    )
    return sku_matches and quantity_matches and segment_matches


def select_discount(rules, customer, sku, quantity):
    eligible = [
        rule
        for rule in rules
        if _eligible(rule, customer, sku, quantity)
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda rule: int(rule.get("percent_bps", 0)))
