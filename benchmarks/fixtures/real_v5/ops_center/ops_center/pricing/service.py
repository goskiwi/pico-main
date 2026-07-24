from ops_center.pricing.discounts import select_discount
from ops_center.pricing.ledger import discounted_total, subtotal


def quote(catalog, rules, customer, sku, quantity):
    unit_cents = catalog[sku]
    subtotal_cents = subtotal(unit_cents, quantity)
    rule = select_discount(rules, customer, sku, quantity)
    discount_bps = int(rule.get("percent_bps", 0)) if rule else 0
    total_cents = discounted_total(subtotal_cents, discount_bps)
    return {
        "sku": sku,
        "quantity": quantity,
        "subtotal_cents": subtotal_cents,
        "discount_bps": discount_bps,
        "rule_id": None if rule is None else rule.get("id"),
        "total_cents": total_cents,
    }
