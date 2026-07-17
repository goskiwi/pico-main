import pytest

from name_index import NameIndex


def populated_index():
    index = NameIndex()
    index.add(1, "one")
    index.add(2, "two")
    return index


def test_rename_to_current_name_is_no_op():
    index = populated_index()
    index.rename(1, "one")
    assert index.resolve("one") == 1
    assert index.name_for(1) == "one"
    assert len(index) == 2


def test_duplicate_target_preserves_both_lookups():
    index = populated_index()
    with pytest.raises(ValueError):
        index.rename(1, "two")
    assert index.name_for(1) == "one"
    assert index.name_for(2) == "two"
    assert index.resolve("one") == 1
    assert index.resolve("two") == 2


def test_missing_id_raises_without_mutation():
    index = populated_index()
    with pytest.raises(KeyError):
        index.rename(99, "missing")
    assert index.resolve("one") == 1
    assert index.resolve("two") == 2


def test_successful_and_repeated_renames_remove_old_names():
    index = populated_index()
    index.rename(1, "first")
    index.rename(1, "primary")
    with pytest.raises(KeyError):
        index.resolve("one")
    with pytest.raises(KeyError):
        index.resolve("first")
    assert index.resolve("primary") == 1
    assert index.name_for(1) == "primary"
    assert len(index) == 2
