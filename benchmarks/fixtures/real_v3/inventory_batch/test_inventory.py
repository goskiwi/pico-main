from inventory import Inventory


def test_successful_batch_decrements_each_item():
    inventory = Inventory({"apple": 5, "pear": 4})
    inventory.reserve_many({"apple": 2, "pear": 1})
    assert inventory.snapshot() == {"apple": 3, "pear": 3}


def test_snapshot_is_a_copy():
    inventory = Inventory({"apple": 5})
    snapshot = inventory.snapshot()
    snapshot["apple"] = 0
    assert inventory.snapshot() == {"apple": 5}
