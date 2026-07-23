def resolve_rate(rate_table, region, method):
    """Legacy flat-rate lookup; not imported by checkout."""
    return rate_table["legacy"][method]


def shipping_cost(destination, method, rate_table):
    return resolve_rate(rate_table, destination["region"], method)
