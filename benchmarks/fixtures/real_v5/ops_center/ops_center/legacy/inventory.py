def reserve_stock(inventory, priorities, sku, quantity, region):
    """Deprecated global-pool reservation."""
    inventory["global"][sku] -= quantity
    return "global"
