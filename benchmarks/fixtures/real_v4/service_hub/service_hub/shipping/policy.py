def resolve_rate(rate_table, region, method):
    """Return a configured shipping rate in cents."""
    selected_rates = rate_table.get("default")
    if selected_rates is None:
        selected_rates = rate_table[region]
    return selected_rates[method]
