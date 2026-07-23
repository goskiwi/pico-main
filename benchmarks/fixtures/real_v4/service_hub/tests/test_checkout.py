from service_hub.checkout.api import quote_order


def test_quote_order_without_a_default_rate_table():
    order = {
        "items": [{"unit_cents": 250, "quantity": 2}],
        "destination": {"region": "us"},
        "shipping_method": "ground",
    }
    assert quote_order(order, {"us": {"ground": 75}}) == {
        "subtotal_cents": 500,
        "shipping_cents": 75,
        "total_cents": 575,
    }
