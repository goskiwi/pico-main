def normalize_sku(value):
    """Return the canonical representation used for inventory keys."""
    if not isinstance(value, str):
        raise TypeError("SKU must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("SKU must not be empty")
    return cleaned
