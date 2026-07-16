import pytest

from inventory import normalize_sku


def test_normalize_sku_preserves_canonical_value():
    assert normalize_sku("SKU-42") == "SKU-42"


def test_normalize_sku_rejects_blank_value():
    with pytest.raises(ValueError):
        normalize_sku("   ")
