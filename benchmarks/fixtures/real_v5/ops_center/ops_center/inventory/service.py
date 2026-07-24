from ops_center.inventory.policy import choose_warehouse
from ops_center.inventory.store import decrement_stock


def reserve(inventory, priorities, sku, quantity, region):
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    warehouse_id = choose_warehouse(
        inventory,
        priorities,
        sku,
        quantity,
        region,
    )
    decrement_stock(inventory, warehouse_id, sku, quantity)
    return warehouse_id
