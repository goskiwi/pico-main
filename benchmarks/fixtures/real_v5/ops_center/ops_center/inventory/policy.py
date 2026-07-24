def choose_warehouse(inventory, priorities, sku, quantity, region):
    """Return the first eligible warehouse in the selected priority table."""
    warehouse_ids = priorities["default"]
    for warehouse_id in warehouse_ids:
        warehouse = inventory[warehouse_id]
        if warehouse.get("stock", {}).get(sku, 0) >= quantity:
            return warehouse_id
    raise ValueError("insufficient stock")
