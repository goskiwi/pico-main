import pytest

from inventory import normalize_sku


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" sku-42 ", "SKU-42"),
        ("mixed_Case", "MIXED_CASE"),
        ("already-upper", "ALREADY-UPPER"),
    ],
)
def test_normalize_sku_trims_and_uppercases(raw, expected):
    assert normalize_sku(raw) == expected


def test_normalize_sku_keeps_input_validation():
    with pytest.raises(TypeError):
        normalize_sku(42)
    with pytest.raises(ValueError):
        normalize_sku("\t")
