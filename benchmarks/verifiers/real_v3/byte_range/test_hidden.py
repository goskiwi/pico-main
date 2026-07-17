import pytest

from range_parser import parse_byte_range


def test_open_ended_range_reaches_resource_end():
    assert parse_byte_range("bytes=7-", 10) == (7, 9)


def test_suffix_range_selects_final_bytes():
    assert parse_byte_range("bytes=-3", 10) == (7, 9)


def test_oversized_suffix_selects_entire_resource():
    assert parse_byte_range("bytes=-20", 10) == (0, 9)


@pytest.mark.parametrize(
    ("header", "size"),
    [
        ("bytes=-0", 10),
        ("bytes=1-2,4-5", 10),
        ("bytes=abc", 10),
        ("bytes=4-2", 10),
        ("bytes=10-", 10),
        ("bytes=0-1", 0),
    ],
)
def test_invalid_or_unsatisfiable_ranges_are_rejected(header, size):
    with pytest.raises(ValueError):
        parse_byte_range(header, size)
