from copy import deepcopy

import pytest

from ops_center.inventory.api import reserve_stock


def _inventory():
    return {
        "west-1": {"stock": {"widget": 10}},
        "east-1": {"stock": {"widget": 2}},
        "east-2": {"stock": {"widget": 8}},
    }


def test_present_region_uses_its_first_sufficient_warehouse():
    inventory = _inventory()
    priorities = {
        "east": ["east-1", "east-2"],
        "default": ["west-1"],
    }

    selected = reserve_stock(
        inventory,
        priorities,
        "widget",
        5,
        "east",
    )

    assert selected == "east-2"
    assert inventory["east-2"]["stock"]["widget"] == 3
    assert inventory["west-1"]["stock"]["widget"] == 10


def test_present_but_insufficient_region_does_not_fall_back():
    inventory = _inventory()
    priorities = {
        "east": ["east-1"],
        "default": ["west-1"],
    }
    before = deepcopy(inventory)

    with pytest.raises(ValueError, match="insufficient stock"):
        reserve_stock(
            inventory,
            priorities,
            "widget",
            5,
            "east",
        )

    assert inventory == before


def test_absent_region_uses_default_list():
    inventory = _inventory()
    priorities = {"default": ["east-1", "west-1"]}

    selected = reserve_stock(
        inventory,
        priorities,
        "widget",
        4,
        "north",
    )

    assert selected == "west-1"
    assert inventory["west-1"]["stock"]["widget"] == 6


def test_unknown_selected_warehouse_is_atomic():
    inventory = _inventory()
    priorities = {
        "east": ["missing-warehouse"],
        "default": ["west-1"],
    }
    before_inventory = deepcopy(inventory)
    before_priorities = deepcopy(priorities)

    with pytest.raises(KeyError):
        reserve_stock(
            inventory,
            priorities,
            "widget",
            1,
            "east",
        )

    assert inventory == before_inventory
    assert priorities == before_priorities


@pytest.mark.parametrize("quantity", [0, -1])
def test_invalid_quantity_is_atomic(quantity):
    inventory = _inventory()
    priorities = {"default": ["west-1"]}
    before = deepcopy(inventory)

    with pytest.raises(ValueError):
        reserve_stock(
            inventory,
            priorities,
            "widget",
            quantity,
            "missing",
        )

    assert inventory == before
