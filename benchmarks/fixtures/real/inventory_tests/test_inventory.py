from inventory import Inventory


def test_reserve_reduces_stock():
    assert Inventory({"A": 3}).reserve("A", 1) == 2
