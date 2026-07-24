from service_hub.checkout.service import merchandise_total
from service_hub.shipping.service import shipping_cost


def quote_order(order, rate_table):
    """Return subtotal, shipping, and total amounts in integer cents."""
    subtotal_cents = merchandise_total(order["items"])
    shipping_cents = shipping_cost(
        order["destination"],
        order["shipping_method"],
        rate_table,
    )
    return {
        "subtotal_cents": subtotal_cents,
        "shipping_cents": shipping_cents,
        "total_cents": subtotal_cents + shipping_cents,
    }
