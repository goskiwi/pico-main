from ops_center.inventory.api import reserve_stock


def test_absent_region_uses_default_warehouse():
    inventory = {
        "west-1": {"stock": {"widget": 5}},
        "east-1": {"stock": {"widget": 2}},
    }
    priorities = {"default": ["west-1"]}

    selected = reserve_stock(
        inventory,
        priorities,
        "widget",
        2,
        "north",
    )

    assert selected == "west-1"
    assert inventory["west-1"]["stock"]["widget"] == 3
