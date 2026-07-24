def select_discount(rules, customer, sku, quantity):
    """Experimental discount combiner, not in production."""
    return {"percent_bps": sum(rule.get("percent_bps", 0) for rule in rules)}
