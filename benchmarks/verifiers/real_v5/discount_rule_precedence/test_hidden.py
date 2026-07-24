from copy import deepcopy

import pytest

from ops_center.pricing.api import quote_invoice


def test_smallest_priority_wins_even_with_a_smaller_discount():
    rules = [
        {
            "id": "large-late",
            "sku": "widget",
            "priority": 20,
            "percent_bps": 4000,
        },
        {
            "id": "small-first",
            "sku": "widget",
            "priority": 5,
            "percent_bps": 1000,
        },
    ]

    quote = quote_invoice(
        {"widget": 333},
        rules,
        {"segment": "retail"},
        "widget",
        3,
    )

    assert quote["rule_id"] == "small-first"
    assert quote["subtotal_cents"] == 999
    assert quote["total_cents"] == 899


def test_priority_tie_preserves_original_rule_order():
    rules = [
        {
            "id": "first",
            "sku": "*",
            "priority": 3,
            "percent_bps": 500,
        },
        {
            "id": "second",
            "sku": "widget",
            "priority": 3,
            "percent_bps": 3000,
        },
    ]

    quote = quote_invoice(
        {"widget": 1000},
        rules,
        {},
        "widget",
        1,
    )

    assert quote["rule_id"] == "first"
    assert quote["total_cents"] == 950


def test_eligibility_combines_sku_quantity_and_segment():
    rules = [
        {
            "id": "wrong-segment",
            "sku": "widget",
            "segment": "enterprise",
            "priority": 1,
            "percent_bps": 5000,
        },
        {
            "id": "too-small",
            "sku": "*",
            "min_qty": 5,
            "priority": 2,
            "percent_bps": 4000,
        },
        {
            "id": "eligible",
            "sku": "*",
            "segment": "retail",
            "min_qty": 2,
            "priority": 9,
            "percent_bps": 1250,
        },
    ]

    quote = quote_invoice(
        {"widget": 200},
        rules,
        {"segment": "retail"},
        "widget",
        2,
    )

    assert quote["rule_id"] == "eligible"
    assert quote["total_cents"] == 350


def test_invalid_selected_percentage_raises_without_mutating_inputs():
    catalog = {"widget": 100}
    rules = [
        {
            "id": "invalid",
            "priority": 1,
            "percent_bps": 10001,
        }
    ]
    customer = {"segment": "retail"}
    before = deepcopy((catalog, rules, customer))

    with pytest.raises(ValueError):
        quote_invoice(
            catalog,
            rules,
            customer,
            "widget",
            1,
        )

    assert (catalog, rules, customer) == before


def test_unknown_sku_and_invalid_quantity_are_rejected():
    with pytest.raises(KeyError):
        quote_invoice({}, [], {}, "missing", 1)
    with pytest.raises(ValueError):
        quote_invoice({"widget": 100}, [], {}, "widget", 0)
