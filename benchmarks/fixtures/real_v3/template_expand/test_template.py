import pytest

from template import expand_template


def test_named_value_is_substituted():
    assert expand_template("hello ${NAME}", {"NAME": "Ada"}) == "hello Ada"


def test_double_dollar_emits_literal_dollar():
    assert expand_template("cost $$5", {}) == "cost $5"


def test_non_string_template_is_rejected():
    with pytest.raises(TypeError):
        expand_template(None, {})
