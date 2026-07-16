import pytest

from records import parse_record


def test_simple_record():
    assert parse_record("alice,42,active") == ("alice", "42", "active")


def test_non_string_rejected():
    with pytest.raises(TypeError):
        parse_record(None)
