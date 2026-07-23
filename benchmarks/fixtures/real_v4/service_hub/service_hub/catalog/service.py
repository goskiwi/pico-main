from service_hub.catalog.cache import invalidate_product_views
from service_hub.catalog.store import rename_slug


def rename_item(catalog, cache, item_id, new_slug):
    """Coordinate a catalog slug rename."""
    old_slug = rename_slug(catalog, item_id, new_slug)
    invalidate_product_views(cache, item_id, new_slug)
    return old_slug
