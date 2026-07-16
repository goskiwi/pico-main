import pytest

from inventory import Inventory, OutOfStockError


def test_reserving_exact_stock_succeeds_and_returns_zero():
    inventory = Inventory({"SKU-1": 3})
    assert inventory.reserve("SKU-1", 3) == 0
    assert inventory.available("SKU-1") == 0


def test_quantity_validation_and_shortage_behavior_are_preserved():
    inventory = Inventory({"SKU-1": 1})
    with pytest.raises(ValueError):
        inventory.reserve("SKU-1", 0)
    with pytest.raises(OutOfStockError):
        inventory.reserve("SKU-1", 2)
