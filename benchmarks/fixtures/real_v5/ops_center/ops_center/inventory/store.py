def decrement_stock(inventory, warehouse_id, sku, quantity):
    warehouse = inventory[warehouse_id]
    stock = warehouse.setdefault("stock", {})
    stock[sku] = stock.get(sku, 0) - quantity
