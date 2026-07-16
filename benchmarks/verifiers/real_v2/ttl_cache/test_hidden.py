import pytest

from cache import TTLCache


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_entry_expires_at_exact_boundary_and_is_removed():
    clock = Clock()
    cache = TTLCache(5, clock=clock)
    cache.set("key", "value")
    clock.now = 104.999
    assert cache.get("key") == "value"
    clock.now = 105.0
    with pytest.raises(KeyError):
        cache.get("key")
    with pytest.raises(KeyError):
        cache.get("key")


def test_overwrite_resets_expiration_deadline():
    clock = Clock()
    cache = TTLCache(5, clock=clock)
    cache.set("key", "old")
    clock.now = 104.0
    cache.set("key", "new")
    clock.now = 105.0
    assert cache.get("key") == "new"
    clock.now = 109.0
    with pytest.raises(KeyError):
        cache.get("key")


@pytest.mark.parametrize("ttl", [0, -0.1, -10])
def test_ttl_must_be_positive(ttl):
    with pytest.raises(ValueError):
        TTLCache(ttl)
