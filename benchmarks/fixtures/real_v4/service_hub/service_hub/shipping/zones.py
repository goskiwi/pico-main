REGION_GROUPS = {
    "north-america": frozenset({"us", "ca"}),
    "europe": frozenset({"de", "fr", "pt"}),
}


def region_group(region):
    """Return a reporting group; checkout pricing does not use this helper."""
    for group, members in REGION_GROUPS.items():
        if region in members:
            return group
    return "other"
