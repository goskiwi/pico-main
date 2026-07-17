import pytest

from name_index import NameIndex


def test_unique_rename_updates_both_lookups():
    index = NameIndex()
    index.add(1, "old")
    index.rename(1, "new")
    assert index.name_for(1) == "new"
    assert index.resolve("new") == 1


def test_add_rejects_duplicate_id():
    index = NameIndex()
    index.add(1, "one")
    with pytest.raises(ValueError):
        index.add(1, "other")
