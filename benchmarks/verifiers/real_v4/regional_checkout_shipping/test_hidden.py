from copy import deepcopy

import pytest

from service_hub.checkout.api import quote_order


def _order(region="eu", method="ground"):
    return {
        "items": [
            {"unit_cents": 350, "quantity": 2},
            {"unit_cents": 125, "quantity": 1},
        ],
        "destination": {"region": region},
        "shipping_method": method,
    }


def test_exact_region_takes_precedence_over_default():
    rates = {
        "eu": {"ground": 240},
        "default": {"ground": 900},
    }
    assert quote_order(_order(), rates) == {
        "subtotal_cents": 825,
        "shipping_cents": 240,
        "total_cents": 1065,
    }


def test_absent_region_falls_back_to_default():
    rates = {"us": {"ground": 180}, "default": {"ground": 500}}
    assert quote_order(_order(region="apac"), rates)["shipping_cents"] == 500


def test_selected_region_does_not_fall_back_per_method():
    rates = {
        "eu": {"express": 700},
        "default": {"ground": 500},
    }
    with pytest.raises(KeyError):
        quote_order(_order(region="eu", method="ground"), rates)


def test_missing_region_without_default_raises_key_error():
    with pytest.raises(KeyError):
        quote_order(_order(region="apac"), {"us": {"ground": 180}})


def test_quote_does_not_mutate_inputs():
    order = _order()
    rates = {"eu": {"ground": 240}, "default": {"ground": 900}}
    original_order = deepcopy(order)
    original_rates = deepcopy(rates)
    quote_order(order, rates)
    assert order == original_order
    assert rates == original_rates
