import pytest

from config_loader import parse_config


def test_non_strict_mode_skips_malformed_lines_and_empty_keys():
    assert parse_config(
        ["host=localhost", "broken line", " =missing", "token=a=b", "port=8080"],
        strict=False,
    ) == {
        "host": "localhost",
        "token": "a=b",
        "port": "8080",
    }


def test_strict_mode_remains_the_default():
    with pytest.raises(ValueError):
        parse_config(["broken line"])
    with pytest.raises(ValueError):
        parse_config(["=missing"])


def test_strict_must_be_keyword_only():
    with pytest.raises(TypeError):
        parse_config(["broken line"], False)
