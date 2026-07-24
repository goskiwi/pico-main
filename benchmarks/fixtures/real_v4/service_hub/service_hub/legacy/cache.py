def invalidate_product_views(cache, item_id, slug):
    """Legacy cache flush; active catalog code does not import this."""
    cache.clear()


def rename_catalog_item(catalog, cache, item_id, new_slug):
    catalog[item_id]["slug"] = new_slug
    cache.clear()
