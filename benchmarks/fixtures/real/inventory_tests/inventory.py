class Inventory:
    def __init__(self, stock):
        self._stock = dict(stock)

    def reserve(self, sku, quantity):
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        available = self._stock.get(sku, 0)
        if quantity > available:
            raise RuntimeError("insufficient stock")
        self._stock[sku] = available - quantity
        return self._stock[sku]
