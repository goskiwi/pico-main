def resolve_rate(rate_table, region, method):
    """Experimental cheapest-rate selector, unused by checkout."""
    candidates = [
        table[method]
        for name, table in rate_table.items()
        if name != "default" and method in table
    ]
    return min(candidates)
