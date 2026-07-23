def invalidate_product_views(cache, item_id, slug):
    """Remove product-id and slug views from a mutable cache."""
    cache.pop(f"product:{item_id}", None)
    cache.pop(f"slug:{slug}", None)
