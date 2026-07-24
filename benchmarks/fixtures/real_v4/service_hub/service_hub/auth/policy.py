def collect_permissions(role_names, role_definitions):
    """Collect the permissions directly declared by each role."""
    permissions = set()
    for role_name in role_names:
        permissions.update(role_definitions[role_name]["permissions"])
    return frozenset(permissions)
