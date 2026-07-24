from service_hub.catalog.service import rename_item


def rename_catalog_item(catalog, cache, item_id, new_slug):
    """Rename one catalog item and invalidate affected cached views."""
    return rename_item(catalog, cache, item_id, new_slug)
