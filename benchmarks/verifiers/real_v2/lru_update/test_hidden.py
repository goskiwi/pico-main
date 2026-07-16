import pytest

from lru import LRUCache


def test_updating_existing_key_makes_it_most_recent():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 10)
    cache.put("c", 3)
    assert cache.get("a") == 10
    assert cache.get("c") == 3
    with pytest.raises(KeyError):
        cache.get("b")


def test_repeated_updates_do_not_duplicate_order_entries():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("a", 2)
    cache.put("a", 3)
    cache.put("b", 4)
    assert len(cache) == 2
    cache.put("c", 5)
    with pytest.raises(KeyError):
        cache.get("a")
    assert cache.get("b") == 4
    assert cache.get("c") == 5
