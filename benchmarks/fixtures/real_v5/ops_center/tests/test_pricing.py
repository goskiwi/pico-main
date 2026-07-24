from ops_center.pricing.api import quote_invoice


def test_single_discount_rule_is_applied():
    quote = quote_invoice(
        {"widget": 250},
        [
            {
                "id": "standard",
                "sku": "widget",
                "priority": 10,
                "percent_bps": 1000,
            }
        ],
        {"segment": "retail"},
        "widget",
        2,
    )

    assert quote["subtotal_cents"] == 500
    assert quote["discount_bps"] == 1000
    assert quote["total_cents"] == 450
