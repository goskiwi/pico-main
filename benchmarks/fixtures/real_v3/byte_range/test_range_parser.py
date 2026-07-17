import pytest

from range_parser import parse_byte_range


def test_explicit_range_uses_inclusive_offsets():
    assert parse_byte_range("bytes=2-5", 10) == (2, 5)


def test_explicit_end_is_clamped_to_resource():
    assert parse_byte_range("bytes=7-99", 10) == (7, 9)


def test_non_byte_unit_is_rejected():
    with pytest.raises(ValueError):
        parse_byte_range("items=1-2", 10)
