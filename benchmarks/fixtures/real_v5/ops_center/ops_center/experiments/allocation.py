def choose_warehouse(inventory, priorities, sku, quantity, region):
    """Experimental least-stock allocation, not used by public APIs."""
    candidates = sorted(inventory)
    return candidates[0]
