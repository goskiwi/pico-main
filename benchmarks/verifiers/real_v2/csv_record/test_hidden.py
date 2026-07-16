import pytest

from records import parse_record


def test_quoted_commas_and_escaped_quotes():
    assert parse_record('"Doe, Jane","said ""hello""",active') == (
        "Doe, Jane",
        'said "hello"',
        "active",
    )


def test_csv_whitespace_rules_are_not_global_strip_rules():
    assert parse_record('  alpha," beta ",gamma  ') == (
        "  alpha",
        " beta ",
        "gamma  ",
    )


def test_multiline_input_is_rejected_as_more_than_one_record():
    with pytest.raises(ValueError):
        parse_record("a,b\nc,d")
