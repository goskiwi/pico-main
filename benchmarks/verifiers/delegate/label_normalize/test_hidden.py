import pytest

from settings import normalize_label


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_normalize_label_rejects_blank_values(value):
    with pytest.raises(ValueError):
        normalize_label(value)


def test_normalize_label_preserves_internal_characters_after_normalizing():
    assert normalize_label(" beta_2 ") == "BETA_2"
