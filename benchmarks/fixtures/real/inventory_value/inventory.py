def normalize_sku(value):
    if not isinstance(value, str):
        raise TypeError("SKU must be a string")
    cleaned = value.strip().upper()
    if not cleaned:
        raise ValueError("SKU must not be empty")
    return cleaned


class Inventory:
    def __init__(self, stock):
        self._stock = {normalize_sku(sku): int(quantity) for sku, quantity in stock.items()}

    def available(self, sku):
        return self._stock.get(normalize_sku(sku), 0)
