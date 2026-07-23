def rename_slug(catalog, item_id, new_slug):
    """Rename an item in the mutable catalog and return its old slug."""
    item = catalog[item_id]
    old_slug = item["slug"]
    if any(
        other_id != item_id and other["slug"] == new_slug
        for other_id, other in catalog.items()
    ):
        raise ValueError("slug already exists")
    item["slug"] = new_slug
    return old_slug
