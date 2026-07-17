import pytest

from template import expand_template


def test_escaped_placeholder_is_literal():
    assert expand_template("$${HOME}", {"HOME": "/tmp"}) == "${HOME}"


def test_replacement_text_is_not_expanded_again():
    values = {"A": "${B}", "B": "changed"}
    assert expand_template("${A}", values) == "${B}"


def test_adjacent_placeholders_are_expanded_once():
    assert expand_template("${A}${B}", {"A": 1, "B": 2}) == "12"


def test_missing_name_raises_key_error():
    with pytest.raises(KeyError):
        expand_template("hello ${NAME}", {})


@pytest.mark.parametrize("template", ["${NAME", "${BAD-NAME}", "${1NAME}", "${}"])
def test_malformed_placeholder_is_rejected(template):
    with pytest.raises(ValueError):
        expand_template(template, {})


def test_other_lone_dollars_stay_literal():
    assert expand_template("$5 and end$", {}) == "$5 and end$"
