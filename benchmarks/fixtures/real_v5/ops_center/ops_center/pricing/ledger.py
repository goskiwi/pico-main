def subtotal(unit_cents, quantity):
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    return int(unit_cents) * int(quantity)


def discounted_total(subtotal_cents, discount_bps):
    if not 0 <= discount_bps <= 10000:
        raise ValueError("percent_bps must be in 0..10000")
    return subtotal_cents * (10000 - discount_bps) // 10000
