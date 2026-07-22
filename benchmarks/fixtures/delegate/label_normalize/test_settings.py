import pytest

from settings import normalize_label


def test_normalize_label_strips_and_uppercases():
    assert normalize_label("  release-candidate  ") == "RELEASE-CANDIDATE"


def test_normalize_label_rejects_non_strings():
    with pytest.raises(TypeError):
        normalize_label(None)
