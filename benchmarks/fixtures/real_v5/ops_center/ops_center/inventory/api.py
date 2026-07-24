from ops_center.inventory.service import reserve


def reserve_stock(inventory, priorities, sku, quantity, region):
    """Reserve stock through the public inventory workflow."""
    return reserve(inventory, priorities, sku, quantity, region)
