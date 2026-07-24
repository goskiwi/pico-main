from service_hub.shipping.policy import resolve_rate


def shipping_cost(destination, method, rate_table):
    """Resolve the shipping cost for a serialized destination."""
    return resolve_rate(rate_table, destination["region"], method)
