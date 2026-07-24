from checkout import quote


def test_quote_uses_standard_pricing():
    assert quote(10) == {"total": 20, "label": "standard"}
