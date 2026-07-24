from copy import deepcopy

import pytest

from service_hub.catalog.api import rename_catalog_item


def _catalog():
    return {
        "p1": {"slug": "old-name", "title": "First"},
        "p2": {"slug": "other-name", "title": "Second"},
    }


def _cache():
    return {
        "product:p1": {"slug": "old-name"},
        "slug:old-name": {"id": "p1"},
        "slug:new-name": {"id": "stale"},
        "product:p2": {"slug": "other-name"},
        "search:all": ["p1", "p2"],
    }


def test_success_invalidates_old_new_and_id_views_only():
    catalog = _catalog()
    cache = _cache()

    assert rename_catalog_item(catalog, cache, "p1", "new-name") == "old-name"
    assert catalog["p1"]["slug"] == "new-name"
    assert cache == {
        "product:p2": {"slug": "other-name"},
        "search:all": ["p1", "p2"],
    }


def test_missing_cache_entries_are_harmless():
    catalog = _catalog()
    cache = {"unrelated": 1}
    rename_catalog_item(catalog, cache, "p1", "new-name")
    assert cache == {"unrelated": 1}


def test_current_slug_is_a_complete_no_op():
    catalog = _catalog()
    cache = _cache()
    original_catalog = deepcopy(catalog)
    original_cache = deepcopy(cache)

    assert rename_catalog_item(catalog, cache, "p1", "old-name") == "old-name"
    assert catalog == original_catalog
    assert cache == original_cache


def test_conflicting_slug_leaves_catalog_and_cache_unchanged():
    catalog = _catalog()
    cache = _cache()
    original_catalog = deepcopy(catalog)
    original_cache = deepcopy(cache)

    with pytest.raises(ValueError):
        rename_catalog_item(catalog, cache, "p1", "other-name")
    assert catalog == original_catalog
    assert cache == original_cache


def test_unknown_item_leaves_catalog_and_cache_unchanged():
    catalog = _catalog()
    cache = _cache()
    original_catalog = deepcopy(catalog)
    original_cache = deepcopy(cache)

    with pytest.raises(KeyError):
        rename_catalog_item(catalog, cache, "missing", "new-name")
    assert catalog == original_catalog
    assert cache == original_cache
