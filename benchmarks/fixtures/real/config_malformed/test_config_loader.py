import pytest

from config_loader import parse_config


def test_parse_config_is_strict_by_default():
    with pytest.raises(ValueError):
        parse_config(["host=localhost", "broken line"])


def test_parse_config_accepts_empty_values():
    assert parse_config(["token="]) == {"token": ""}
