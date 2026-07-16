from decimal import Decimal

import pytest

from inventory import Inventory


def test_total_value_uses_decimal_and_normalized_skus():
    inventory = Inventory({" sku-a ": 2, "SKU-B": 3})
    result = inventory.total_value({"SKU-A": "1.25", " sku-b ": Decimal("2.00")})
    assert result == Decimal("8.50")
    assert isinstance(result, Decimal)


def test_total_value_handles_empty_inventory():
    assert Inventory({}).total_value({}) == Decimal("0")


def test_total_value_rejects_missing_price():
    with pytest.raises(KeyError):
        Inventory({"SKU-A": 1}).total_value({})
