from checkout.pricing import calculate_total


def quote(amount):
    return {
        "total": calculate_total(amount),
        "label": "standard",
    }
