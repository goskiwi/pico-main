import pytest

from inventory import Inventory, OutOfStockError


def test_reserve_reduces_available_stock():
    inventory = Inventory({"SKU-1": 5})
    assert inventory.reserve("SKU-1", 2) == 3


def test_reserve_rejects_more_than_available():
    inventory = Inventory({"SKU-1": 2})
    with pytest.raises(OutOfStockError):
        inventory.reserve("SKU-1", 3)
