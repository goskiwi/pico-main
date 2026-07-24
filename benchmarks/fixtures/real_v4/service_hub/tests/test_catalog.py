from service_hub.catalog.api import rename_catalog_item


def test_rename_updates_the_catalog():
    catalog = {"p1": {"slug": "old-name"}, "p2": {"slug": "other"}}
    old_slug = rename_catalog_item(catalog, {}, "p1", "new-name")
    assert old_slug == "old-name"
    assert catalog["p1"]["slug"] == "new-name"
