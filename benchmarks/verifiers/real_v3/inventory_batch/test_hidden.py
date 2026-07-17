import pytest

from inventory import Inventory


def test_late_insufficient_item_rolls_back_entire_batch():
    inventory = Inventory({"apple": 5, "pear": 1})
    with pytest.raises(ValueError):
        inventory.reserve_many({"apple": 2, "pear": 2})
    assert inventory.snapshot() == {"apple": 5, "pear": 1}


def test_late_unknown_item_rolls_back_entire_batch():
    inventory = Inventory({"apple": 5})
    with pytest.raises(KeyError):
        inventory.reserve_many({"apple": 2, "missing": 1})
    assert inventory.snapshot() == {"apple": 5}


@pytest.mark.parametrize("quantity", [0, -1, 1.5, True])
def test_invalid_quantity_does_not_change_stock(quantity):
    inventory = Inventory({"apple": 5})
    with pytest.raises(ValueError):
        inventory.reserve_many({"apple": quantity})
    assert inventory.snapshot() == {"apple": 5}


def test_exact_and_empty_reservations_succeed():
    inventory = Inventory({"apple": 2, "pear": 3})
    inventory.reserve_many({})
    inventory.reserve_many({"apple": 2})
    assert inventory.snapshot() == {"apple": 0, "pear": 3}
