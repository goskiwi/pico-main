from inventory import Inventory


def test_available_uses_normalized_sku():
    assert Inventory({" sku-1 ": 2}).available("SKU-1") == 2
