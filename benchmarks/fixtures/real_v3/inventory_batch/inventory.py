class Inventory:
    def __init__(self, stock):
        self._stock = dict(stock)

    def reserve_many(self, requested):
        """Reserve all requested quantities."""
        for sku, quantity in requested.items():
            if not isinstance(quantity, int) or quantity <= 0:
                raise ValueError("quantity must be a positive integer")
            available = self._stock[sku]
            if quantity > available:
                raise ValueError("insufficient stock")
            self._stock[sku] = available - quantity

    def snapshot(self):
        return dict(self._stock)
