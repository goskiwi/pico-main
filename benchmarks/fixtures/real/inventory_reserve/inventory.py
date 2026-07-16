class OutOfStockError(RuntimeError):
    pass


class Inventory:
    def __init__(self, stock):
        self._stock = dict(stock)

    def available(self, sku):
        return self._stock.get(sku, 0)

    def reserve(self, sku, quantity):
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        available = self.available(sku)
        if available <= quantity:
            raise OutOfStockError(sku)
        self._stock[sku] = available - quantity
        return self._stock[sku]
