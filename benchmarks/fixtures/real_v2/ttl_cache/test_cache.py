from cache import TTLCache


def test_round_trip_before_expiry():
    cache = TTLCache(10, clock=lambda: 1)
    cache.set("token", "abc")
    assert cache.get("token") == "abc"


def test_missing_key_raises_key_error():
    cache = TTLCache(10, clock=lambda: 1)
    try:
        cache.get("missing")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")
