import pytest

from lru import LRUCache


def test_get_marks_key_as_recently_used():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1
    cache.put("c", 3)
    with pytest.raises(KeyError):
        cache.get("b")


def test_capacity_validation():
    with pytest.raises(ValueError):
        LRUCache(0)
