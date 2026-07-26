import pytest

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version


@pytest.mark.parametrize("value", [None, 1, ["1", ".", "0"], ("1",), b"1.0"])
def test_non_string_versions_raise_invalid_version(value):
    with pytest.raises(InvalidVersion):
        Version(value)


def test_filter_skips_keyed_none_version():
    items = [{"version": "1.0"}, {"version": None}, {"version": "2.0"}]

    assert list(SpecifierSet(">=1").filter(items, key=lambda item: item["version"])) == [
        {"version": "1.0"},
        {"version": "2.0"},
    ]
